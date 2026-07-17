# Transcription forwarder infrastructure

Event-driven pipeline that forwards provider transcriptions (dropped in the
landing S3 bucket via cross-account replication) to the EmpatIA API.

Naming is derived from `stack_id` (e.g. `augusta-nexa-dev`, whose trailing
segment `dev` is the environment):

| Thing | Value |
| --- | --- |
| Landing bucket | `<stack_id>-providers-landing` → `augusta-nexa-dev-providers-landing` |
| S3 landing prefix | `transacciones/empatia/transcripciones/detalle/` |
| SSM base path | `/<stack_id>/empatia/transcripciones/detalle` → `/augusta-nexa-dev/empatia/transcripciones/detalle` |

```
Accenture account                Our account
┌──────────────┐   S3 repl   ┌───────────────────────────┐
│ source bucket │ ──────────▶ │ augusta-nexa-dev-providers │
└──────────────┘             │        -landing            │
                             └──────────────┬─────────────┘
                              Object Created │  key: transacciones/empatia/
                                             │       transcripciones/detalle/BDO/...
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
          SSM .../detalle/<CLIENT>   Secrets Manager        S3 GetObject
          (String, JSON: routing)    <client>_detalle       (read payload)
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

| Store | Path | Holds |
| --- | --- | --- |
| Secrets Manager | `/augusta-nexa-dev/empatia/api/<client>_detalle` | `client_id`, `client_secret`, `grant_type` |
| Parameter Store | `/augusta-nexa-dev/empatia/transcripciones/detalle/<CLIENT>` | `token_url`, `api_base_url`, `endpoint_path`, `bucket_prefix`, `secret_name`, `enabled` |

### 1. Credentials — Secrets Manager (one secret per client)

Name: `/augusta-nexa-dev/empatia/api/bdo_detalle` (encrypted with the
`augusta-nexa-dev` CMK). Managed **outside** this module — Terraform only reads it.

```json
{
  "client_id": "Connection.Apis.Auth",
  "client_secret": "REPLACE_WITH_REAL_CLIENT_SECRET",
  "grant_type": "client_credentials"
}
```

The name is derived from the client key: `BDO` -> `bdo_detalle`
(`lower(<key>) + "_detalle"`). Override per client with `secret_name` if it differs.

### 2. Routing — Parameter Store (one `String` param per client)

Name: `/augusta-nexa-dev/empatia/transcripciones/detalle/BDO` — created by Terraform
from the `clients` map.

```json
{
  "token_url": "https://login-server-staging.nexabpo.com/auth/realms/nexa/protocol/openid-connect/token",
  "api_base_url": "https://nexa-empatia-staging.nexabpo.com/transcription/api/tipificaciones",
  "endpoint_path": "banco_occ",
  "bucket_prefix": "transacciones/empatia/transcripciones/detalle/BDO/",
  "secret_name": "/augusta-nexa-dev/empatia/api/bdo_detalle",
  "enabled": true
}
```

| Field | Meaning |
| --- | --- |
| `token_url` | Keycloak client-credentials token endpoint |
| `api_base_url` | Base URL of the EmpatIA tipificaciones API (no trailing endpoint) |
| `endpoint_path` | Last URL segment for this client (`banco_occ` for Banco de Occidente) |
| `bucket_prefix` | Expected S3 prefix; objects outside it are rejected |
| `secret_name` | Secrets Manager secret holding this client's credentials |
| `enabled` | `false` pauses forwarding without deleting anything |

---

## How one file flows end to end

```
S3 key:  transacciones/empatia/transcripciones/detalle/BDO/2026/07/13/call.json
         └──────────────── landing prefix ───────────────┘└┬┘
                                               client_key ─┘ = "BDO"
                                                            │
SSM   :  /augusta-nexa-dev/empatia/transcripciones/detalle/BDO
             ├─ bucket_prefix  -> verify the key belongs to this client
             ├─ secret_name    -> /augusta-nexa-dev/empatia/api/bdo_detalle
             │                     └─ Secrets Manager: client_id / client_secret / grant_type
             ├─ token_url      -> POST creds -> access_token   (cached per secret)
             └─ api_base_url + "/" + endpoint_path
                                                            │
POST  :  https://nexa-empatia-staging.nexabpo.com/transcription/api/tipificaciones/banco_occ
```

Tokens are cached **per secret**, so clients never share each other's tokens.

---

## Deploy

**Prerequisite:** each client's secret must already exist in Secrets Manager
(Terraform reads it, it does not create it). For `BDO`:
`/augusta-nexa-dev/empatia/api/bdo_detalle`.

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
  --secret-id "/augusta-nexa-dev/empatia/api/bdo_detalle" \
  --secret-string '{"client_id":"Connection.Apis.Auth","client_secret":"REAL_SECRET_HERE","grant_type":"client_credentials"}'

# BDO (Banco de Occidente) -> banco_occ routing (normally Terraform-managed)
aws ssm put-parameter \
  --name "/augusta-nexa-dev/empatia/transcripciones/detalle/BDO" \
  --type String \
  --overwrite \
  --value '{"token_url":"https://login-server-staging.nexabpo.com/auth/realms/nexa/protocol/openid-connect/token","api_base_url":"https://nexa-empatia-staging.nexabpo.com/transcription/api/tipificaciones","endpoint_path":"banco_occ","bucket_prefix":"transacciones/empatia/transcripciones/detalle/BDO/","secret_name":"/augusta-nexa-dev/empatia/api/bdo_detalle","enabled":true}'
```

The Lambda caches parameters in memory for `CONFIG_TTL_SECONDS` (default 300s),
so a manual change is picked up within ~5 minutes without a redeploy.

---

## Adding a new endpoint (e.g. Banco de Bogota)

1. Create the client's secret first (it must exist before `apply`):
   `/augusta-nexa-dev/empatia/api/bdb_detalle`
2. Add an entry to the `clients` map in `terraform.tfvars`. The map key is the
   **S3 folder**; `endpoint_path` is the **last segment of the API URL**; the
   secret name is derived from the key (`BDB` -> `bdb_detalle`):
   ```hcl
   BDB = {
     token_url     = "https://login-server-staging.nexabpo.com/auth/realms/nexa/protocol/openid-connect/token"
     api_base_url  = "https://nexa-empatia-staging.nexabpo.com/transcription/api/tipificaciones"
     endpoint_path = "banco_bogota"
     enabled       = true
   }
   ```
2. `terraform apply` — creates `/augusta-nexa-dev/empatia/transcripciones/detalle/BDB`
   and extends the EventBridge rule to route the
   `transacciones/empatia/transcripciones/detalle/BDB/` prefix.
3. Providers drop files under `.../detalle/BDB/...`. **No Lambda change or redeploy.**

---

## Testing the deployed Lambda

**Option A — drop a file in S3** (full end-to-end):
```bash
aws s3 cp sample.json \
  s3://augusta-nexa-dev-providers-landing/transacciones/empatia/transcripciones/detalle/BDO/2026/07/13/sample.json
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
