# Transcription forwarder infrastructure

Event-driven pipeline that forwards provider transcriptions (dropped in the
landing S3 bucket via cross-account replication) to the EmpatIA API.

```
Accenture account                Our account
┌──────────────┐   S3 repl   ┌───────────────────────────┐
│ source bucket │ ──────────▶ │ augusta-nexa-dev-providers │
└──────────────┘             │        -landing            │
                             └──────────────┬─────────────┘
                                            │ Object Created
                                            ▼
                                     ┌──────────────┐
                                     │ EventBridge  │  (rule: known client prefixes)
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
                 SSM /keycloak      SSM /clients/<key>    S3 GetObject
                 (SecureString)     (String, JSON)        (read payload)
                        │                                       │
                        └──────────── POST Bearer token ────────┴──▶ EmpatIA API
```

## Layout

```
terraform/
  modules/transcription-forwarder/   # reusable module (sqs, dlq, eventbridge, lambda, ssm, iam)
  environments/dev/                  # dev composition -> module call + tfvars
```

---

## Parameter Store — JSON formats

All configuration lives in SSM Parameter Store as **JSON documents**. There are
two kinds of parameter.

### 1. Shared Keycloak credentials — `SecureString`

Path: `/nexa/empatia/<env>/keycloak` (e.g. `/nexa/empatia/dev/keycloak`)

```json
{
  "token_url": "https://login-server-staging.nexabpo.com/auth/realms/nexa/protocol/openid-connect/token",
  "client_id": "Connection.Apis.Auth",
  "client_secret": "REPLACE_WITH_REAL_CLIENT_SECRET"
}
```

- Stored as a **SecureString** (KMS-encrypted at rest). The Lambda reads it with
  `WithDecryption=true`.
- One shared credential is used for all endpoints (the API distinguishes clients
  by URL, not by token).
- ⚠️ **Never commit the real `client_secret`.** Inject it at apply time via
  `TF_VAR_keycloak_config` (see below) or set the parameter directly with the
  AWS CLI. The value shown here is a placeholder.

### 2. Per-client endpoint routing — `String`

Path: `/nexa/empatia/<env>/clients/<client_key>`

The `<client_key>` **is the client name** and **must match the first path segment
of the S3 object key**. For Banco de Occidente the client name is `banco_occ`, so:

- Parameter name: `/nexa/empatia/dev/clients/banco_occ`
- Files land at: `s3://augusta-nexa-dev-providers-landing/banco_occ/2026/07/13/call-123.json`

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

---

## How the client name flows end to end

```
S3 key:            banco_occ/2026/07/13/call-123.json
                   └────┬────┘
client_key  ───────────┘  (Lambda: key.split("/", 1)[0])
                          │
SSM lookup: /nexa/empatia/dev/clients/banco_occ
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

Terraform creates the SSM parameters for you from `var.keycloak_config` and
`var.clients`, so you normally do **not** touch Parameter Store by hand.

### Setting / rotating a parameter manually (AWS CLI)

If you prefer to manage a value outside Terraform (e.g. rotating the secret):

```bash
# Keycloak credentials (encrypted)
aws ssm put-parameter \
  --name "/nexa/empatia/dev/keycloak" \
  --type SecureString \
  --overwrite \
  --value '{"token_url":"https://login-server-staging.nexabpo.com/auth/realms/nexa/protocol/openid-connect/token","client_id":"Connection.Apis.Auth","client_secret":"REAL_SECRET_HERE"}'

# banco_occ endpoint routing
aws ssm put-parameter \
  --name "/nexa/empatia/dev/clients/banco_occ" \
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
2. `terraform apply` — creates `/nexa/empatia/dev/clients/banco_bogota` and extends
   the EventBridge rule to route the `banco_bogota/` prefix.
3. Providers start dropping files under `banco_bogota/...`. **No Lambda change or redeploy.**

---

## Testing the deployed Lambda

**Option A — drop a file in S3** (full end-to-end):
```bash
aws s3 cp sample.json s3://augusta-nexa-dev-providers-landing/banco_occ/2026/07/13/sample.json
```
Then watch the Lambda logs and the DLQ.

**Option B — console test event**: use
[`../lambdas/empatia-tipificaciones/test_event.json`](../lambdas/empatia-tipificaciones/test_event.json),
an SQS-wrapped EventBridge "Object Created" event pointing at a `banco_occ/` key.

---

## Notes

- `aws_s3_bucket_notification` is **authoritative** for the landing bucket. If that
  bucket already has notifications managed elsewhere, set
  `manage_bucket_notification = false` and enable EventBridge in that other config.
- SQS visibility timeout is set to 6× the Lambda timeout; the Lambda reports
  `ReportBatchItemFailures` so only failed records are retried / sent to the DLQ.
- Poison messages (bad JSON, permanent API errors) land in the DLQ after
  `max_receive_count` (default 5) attempts — alarm on `ApproximateNumberOfMessagesVisible`.
