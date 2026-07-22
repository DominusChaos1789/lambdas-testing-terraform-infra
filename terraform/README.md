# Transcription forwarder infrastructure

Event-driven pipeline that forwards provider transcriptions (dropped in the
landing S3 bucket via cross-account replication) to the EmpatIA API.

Naming is derived from `stack_id` (e.g. `augusta-nexa-dev`, whose trailing
segment `dev` is the environment):

| Thing | Value |
| --- | --- |
| Landing bucket | `<stack_id>-providers-landing` → `augusta-nexa-dev-providers-landing` |
| S3 landing prefix | `external/transacciones/empatia/transcripciones/` |
| SSM param + secret | `/<stack_id>/empatia/api/<client>-detalle` → `/augusta-nexa-dev/empatia/api/bdo-detalle` |

```
Accenture account                Our account
┌──────────────┐   S3 repl   ┌───────────────────────────┐
│ source bucket │ ──────────▶ │ augusta-nexa-dev-providers │
└──────────────┘             │        -landing            │
                             └──────────────┬─────────────┘
                              Object Created │  key: external/transacciones/empatia/
                                             │  transcripciones/BDO/year=…/…
                                             ▼
                                     ┌──────────────┐
                                     │ EventBridge  │  (rule: <prefix>/<client>/ )
                                     └──────┬───────┘
                                            ▼
                                 ┌────────────────────┐   maxReceiveCount   ┌─────┐
                                 │ SQS transcriptions │ ──────────────────▶ │ DLQ │
                                 └─────────┬──────────┘                     └─────┘
                                           │ event source mapping (batch, partial failures)
                                           ▼
                                 ┌──────────────────┐
                                 │ Lambda forwarder │
                                 └─────────┬────────┘
                        ┌──────────────────┼───────────────────┐
                        ▼                  ▼                   ▼
          SSM .../api/<client>-detalle   Secrets Manager    S3 GetObject
          (String, JSON: routing)        <client>-detalle   (read payload)
                                         (client_id/secret)
                        │                  │                      │
                        └───────── POST Bearer token ─────────────┴──▶ EmpatIA API
```

## Layout

```
terraform/
  modules/transcription-forwarder/   # reusable module (sqs, dlq, eventbridge, lambda, ssm, iam)
  environments/dev/                  # dev composition -> module call + tfvars
```

---

## Configuration: Secrets Manager + Parameter Store

Config is split by sensitivity. **Credentials never live in Parameter Store or
Terraform state.**

Both the routing parameter and the secret share the name `<client>-detalle`
under `/<stack_id>/empatia/api` (e.g. `BDO` → `bdo-detalle`).

| Store | Path | Holds |
| --- | --- | --- |
| Secrets Manager | `/<stack>/empatia/api/<client>-detalle` | `client_id`, `client_secret`, `grant_type`, and (optional) `cypher_code` |
| Parameter Store | `/<stack>/empatia/api/<client>-detalle` | `token_url`+`token_path`, `api_url`+`api_path`, `endpoint_path`, `enpoint_cypher_path`, `bucket_prefix`, `secret_name`, `enabled` |

### 1. Credentials — Secrets Manager (one secret per client)

Name: `/augusta-nexa-dev/empatia/api/bdo-detalle` (encrypted with the
`augusta-nexa-dev` CMK). Managed **outside** this module — Terraform only reads it.

```json
{
  "client_id": "Connection.Apis.Auth",
  "client_secret": "REPLACE_WITH_REAL_CLIENT_SECRET",
  "grant_type": "client_credentials",
  "cypher_code": "REPLACE_WITH_BASE64_AES256_KEY"
}
```

`cypher_code` is only needed for clients that use encrypted delivery.

### 2. Routing — Parameter Store (one `String` param per client)

Name: `/augusta-nexa-dev/empatia/api/bdo-detalle` — created by Terraform from the
`clients` map. URLs are split into host + path so environments vary them freely.

```json
{
  "token_url": "https://login-server-staging.nexabpo.com",
  "token_path": "auth/realms/nexa/protocol/openid-connect/token",
  "api_url": "https://nexa-empatia-staging.nexabpo.com",
  "api_path": "transcription/api/tipificaciones",
  "endpoint_path": "banco_occ",
  "enpoint_cypher_path": "bboc_encrip",
  "bucket_prefix": "external/transacciones/empatia/transcripciones/BDO/",
  "secret_name": "empatia/api/bdo-detalle",
  "enabled": true
}
```

| Field | Meaning |
| --- | --- |
| `token_url` + `token_path` | Keycloak token endpoint = `token_url/token_path` |
| `api_url` + `api_path` | EmpatIA API base = `api_url/api_path` |
| `endpoint_path` | Last URL segment for the plaintext POST (`banco_occ`) |
| `enpoint_cypher_path` | Last URL segment for the encrypted POST (`bboc_encrip`); omit to disable |
| `bucket_prefix` | Expected S3 prefix; objects outside it are rejected |
| `secret_name` | **Environment-relative** secret name; the Lambda prepends `/<stack>/` |
| `enabled` | `false` pauses forwarding without deleting anything |

> The `secret_name` is stored without the `/<stack>/` prefix so the same config
> promotes across `augusta-nexa-dev` / `-stg` / `-pro`. The `enpoint_cypher_path`
> field name matches the config's spelling (the Lambda also accepts the corrected
> `endpoint_cypher_path`).

---

## How one file flows end to end

```
S3 key:  external/transacciones/empatia/transcripciones/BDO/year=2026/month=07/day=13/call.json
         └──────────────── landing prefix ───────────────┘└┬┘
                                               client_key ─┘ = "BDO"
                                                            │
SSM   :  /augusta-nexa-dev/empatia/api/bdo-detalle   (STACK_ID + "bdo" + "-detalle")
             ├─ bucket_prefix   -> verify the key belongs to this client
             ├─ secret_name     -> /<stack>/empatia/api/bdo-detalle
             │      └─ Secrets Manager: client_id / client_secret / grant_type / cypher_code
             ├─ token_url+path  -> POST creds -> access_token   (cached per secret)
             └─ api_url + api_path
                                                            │
POST 1 : .../tipificaciones/banco_occ      <- plaintext payload
POST 2 : .../tipificaciones/bboc_encrip    <- {"payload": AES-256(payload)}  (only if enpoint_cypher_path + cypher_code set)
```

Tokens are cached **per secret**, so clients never share each other's tokens.

### Encrypted delivery

When the parameter defines `enpoint_cypher_path` **and** the secret defines
`cypher_code`, the Lambda also POSTs an encrypted copy to
`api_url/api_path/enpoint_cypher_path` as `{"payload": "<base64>"}`.

> ⚠️ The cipher is **AES-256-CBC** (random 16-byte IV prepended, PKCS7 padding,
> standard base64) — see `_encrypt_payload` in `main.py`. This must match what
> the EmpatIA endpoint decrypts with; if it expects AES-GCM / Fernet / a fixed
> IV, change only that function.

---

## Deploy

**Prerequisite:** each client's secret must already exist in Secrets Manager
(Terraform reads it, it does not create it). For `BDO`:
`/augusta-nexa-dev/empatia/api/bdo-detalle`.

```bash
cd environments/dev
cp terraform.tfvars.example terraform.tfvars     # edit the clients map

terraform init
terraform validate
terraform plan
terraform apply
```

No secrets are passed to Terraform — it only creates the routing parameters from
`var.clients` and grants the Lambda read access to the existing secrets.

### Managing values manually (AWS CLI)

```bash
# Create / rotate a client's credentials (Secrets Manager)
aws secretsmanager put-secret-value \
  --secret-id "/augusta-nexa-dev/empatia/api/bdo-detalle" \
  --secret-string '{"client_id":"Connection.Apis.Auth","client_secret":"REAL_SECRET_HERE","grant_type":"client_credentials","cypher_code":"BASE64_AES256_KEY"}'

# BDO (Banco de Occidente) -> banco_occ routing (normally Terraform-managed)
aws ssm put-parameter \
  --name "/augusta-nexa-dev/empatia/api/bdo-detalle" \
  --type String \
  --overwrite \
  --value '{"token_url":"https://login-server-staging.nexabpo.com","token_path":"auth/realms/nexa/protocol/openid-connect/token","api_url":"https://nexa-empatia-staging.nexabpo.com","api_path":"transcription/api/tipificaciones","endpoint_path":"banco_occ","enpoint_cypher_path":"bboc_encrip","bucket_prefix":"external/transacciones/empatia/transcripciones/BDO/","secret_name":"empatia/api/bdo-detalle","enabled":true}'
```

The Lambda caches parameters in memory for `CONFIG_TTL_SECONDS` (default 300s),
so a manual change is picked up within ~5 minutes without a redeploy.

---

## Adding a new endpoint (e.g. Banco de Bogota)

1. Create the client's secret first (it must exist before `apply`):
   `/augusta-nexa-dev/empatia/api/bdb-detalle`
2. Add an entry to the `clients` map in `terraform.tfvars`. The map key is the
   **S3 folder** and maps to the param/secret `bdb-detalle`:
   ```hcl
   BDB = {
     token_url     = "https://login-server-staging.nexabpo.com"
     token_path    = "auth/realms/nexa/protocol/openid-connect/token"
     api_url       = "https://nexa-empatia-staging.nexabpo.com"
     api_path      = "transcription/api/tipificaciones"
     endpoint_path = "banco_bogota"
     enabled       = true
   }
   ```
3. `terraform apply` — creates `/augusta-nexa-dev/empatia/api/bdb-detalle`
   and extends the EventBridge rule to route the
   `external/transacciones/empatia/transcripciones/BDB/` prefix.
4. Providers drop files under `.../transcripciones/BDB/...`. **No Lambda change or redeploy.**

---

## Testing the deployed Lambda

**Option A — drop a file in S3** (full end-to-end):
```bash
aws s3 cp sample.json \
  s3://augusta-nexa-dev-providers-landing/external/transacciones/empatia/transcripciones/BDO/2026/07/13/sample.json
```

**Option B — console test event**: use
[`../lambdas/empatia-tipificaciones/test_event.json`](../lambdas/empatia-tipificaciones/test_event.json),
an SQS-wrapped EventBridge "Object Created" event pointing at a `banco_occ/` key.

---

## Notes

- `aws_s3_bucket_notification` is **authoritative** for the landing bucket. If it
  already has notifications managed elsewhere, set `manage_bucket_notification = false`
  and enable EventBridge in that other config.
- SQS visibility timeout is 6× the Lambda timeout; the Lambda reports
  `ReportBatchItemFailures` so only failed records are retried / sent to the DLQ.
- Poison messages land in the DLQ after `max_receive_count` (default 5) attempts —
  alarm on the DLQ's `ApproximateNumberOfMessagesVisible`.
