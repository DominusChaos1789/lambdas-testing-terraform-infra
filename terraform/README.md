# Transcription forwarder infrastructure

Event-driven pipeline that forwards provider transcriptions (dropped in the
landing S3 bucket via cross-account replication) to the EmpatIA API.

Naming is derived from `stack_id` (e.g. `augusta-nexa-dev`, whose trailing
segment `dev` is the environment):

| Thing | Value |
| --- | --- |
| Landing bucket | `<stack_id>-providers-landing` → `augusta-nexa-dev-providers-landing` |
| S3 landing prefix | `transacciones/empatia/api/transcripciones/detalle/` |
| SSM base path | `/<stack_id>/empatia/transcripciones/detalle` → `/augusta-nexa-dev/empatia/transcripciones/detalle` |

```
Accenture account                Our account
┌──────────────┐   S3 repl   ┌───────────────────────────┐
│ source bucket │ ──────────▶ │ augusta-nexa-dev-providers │
└──────────────┘             │        -landing            │
                             └──────────────┬─────────────┘
                              Object Created │  key: transacciones/empatia/api/
                                             │       transcripciones/detalle/banco_occ/...
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
            SSM .../detalle/keycloak  SSM .../detalle/<client>  S3 GetObject
               (SecureString)           (String, JSON)          (read payload)
                        │                                             │
                        └────────────── POST Bearer token ────────────┴──▶ EmpatIA API
```

## Layout

```
terraform/
  modules/transcription-forwarder/   # reusable module (sqs, dlq, eventbridge, lambda, ssm, iam)
  environments/dev/                  # dev composition -> module call + tfvars
```

---

## Parameter Store — JSON formats

All configuration lives in SSM Parameter Store as **JSON documents** under the
base path `/augusta-nexa-dev/empatia/transcripciones/detalle`.

### 1. Shared Keycloak credentials — `SecureString`

Path: `/augusta-nexa-dev/empatia/transcripciones/detalle/keycloak`

```json
{
  "token_url": "https://login-server-staging.nexabpo.com/auth/realms/nexa/protocol/openid-connect/token",
  "client_id": "Connection.Apis.Auth",
  "client_secret": "REPLACE_WITH_REAL_CLIENT_SECRET"
}
```

- Stored as a **SecureString** (KMS-encrypted at rest); read with `WithDecryption=true`.
- One shared credential is used for all endpoints.
- ⚠️ **Never commit the real `client_secret`.** Inject it via `TF_VAR_keycloak_config`
  or the AWS CLI. The value above is a placeholder.

### 2. Per-client endpoint routing — `String`

Path: `/augusta-nexa-dev/empatia/transcripciones/detalle/<client_name>`

The `<client_name>` **is the client key** and **must match the S3 path segment
that follows the landing prefix**. For Banco de Occidente the client name is
`banco_occ`:

- Parameter name: `/augusta-nexa-dev/empatia/transcripciones/detalle/banco_occ`
- Files land at: `s3://augusta-nexa-dev-providers-landing/transacciones/empatia/api/transcripciones/detalle/banco_occ/2026/07/13/call-123.json`

```json
{
  "api_base_url": "https://nexa-empatia-staging.nexabpo.com/transcription/api/tipificaciones",
  "endpoint_path": "b_occ",
  "enabled": true
}
```

The Lambda builds the final URL as `api_base_url + "/" + endpoint_path`:

```
https://nexa-empatia-staging.nexabpo.com/transcription/api/tipificaciones/b_occ
```

| Field | Meaning |
| --- | --- |
| `api_base_url` | Base URL of the EmpatIA tipificaciones API (no trailing endpoint) |
| `endpoint_path` | Endpoint suffix for this client (`b_occ` for Banco de Occidente) |
| `enabled` | `false` pauses forwarding for this client without deleting anything |

> Note: `keycloak` is a reserved name under the base path — do not name a client
> `keycloak`.

---

## How the client name flows end to end

```
S3 key:  transacciones/empatia/api/transcripciones/detalle/banco_occ/2026/07/13/call-123.json
         └──────────────── landing prefix ───────────────┘└───┬────┘
                                                     client_key ┘  (strip prefix, take next segment)
                                                              │
SSM lookup: /augusta-nexa-dev/empatia/transcripciones/detalle/banco_occ
                                                              │
final POST: {api_base_url}/{endpoint_path}  ->  .../tipificaciones/b_occ
```

---

## Deploy

```bash
cd environments/dev
cp terraform.tfvars.example terraform.tfvars     # edit clients; keep secrets OUT of this file

# Provide the Keycloak secret via env var (never in a committed file):
export TF_VAR_keycloak_config='{"token_url":"https://login-server-staging.nexabpo.com/auth/realms/nexa/protocol/openid-connect/token","client_id":"Connection.Apis.Auth","client_secret":"REAL_SECRET_HERE"}'

terraform init
terraform validate
terraform plan
terraform apply
```

Terraform creates the SSM parameters from `var.keycloak_config` and `var.clients`,
so you normally do **not** touch Parameter Store by hand.

### Setting / rotating a parameter manually (AWS CLI)

```bash
# Keycloak credentials (encrypted)
aws ssm put-parameter \
  --name "/augusta-nexa-dev/empatia/transcripciones/detalle/keycloak" \
  --type SecureString \
  --overwrite \
  --value '{"token_url":"https://login-server-staging.nexabpo.com/auth/realms/nexa/protocol/openid-connect/token","client_id":"Connection.Apis.Auth","client_secret":"REAL_SECRET_HERE"}'

# banco_occ endpoint routing
aws ssm put-parameter \
  --name "/augusta-nexa-dev/empatia/transcripciones/detalle/banco_occ" \
  --type String \
  --overwrite \
  --value '{"api_base_url":"https://nexa-empatia-staging.nexabpo.com/transcription/api/tipificaciones","endpoint_path":"b_occ","enabled":true}'
```

The Lambda caches parameters in memory for `CONFIG_TTL_SECONDS` (default 300s),
so a manual change is picked up within ~5 minutes without a redeploy.

---

## Adding a new endpoint (e.g. banco_bogota)

1. Add an entry to the `clients` map in `terraform.tfvars`:
   ```hcl
   banco_bogota = {
     api_base_url  = "https://nexa-empatia-staging.nexabpo.com/transcription/api/tipificaciones"
     endpoint_path = "b_bog"
     enabled       = true
   }
   ```
2. `terraform apply` — creates `/augusta-nexa-dev/empatia/transcripciones/detalle/banco_bogota`
   and extends the EventBridge rule to route the
   `transacciones/empatia/api/transcripciones/detalle/banco_bogota/` prefix.
3. Providers drop files under `.../detalle/banco_bogota/...`. **No Lambda change or redeploy.**

---

## Testing the deployed Lambda

**Option A — drop a file in S3** (full end-to-end):
```bash
aws s3 cp sample.json \
  s3://augusta-nexa-dev-providers-landing/transacciones/empatia/api/transcripciones/detalle/banco_occ/2026/07/13/sample.json
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
