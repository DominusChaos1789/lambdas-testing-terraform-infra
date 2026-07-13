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
                                 ┌──────────────────┐    maxReceiveCount   ┌─────┐
                                 │ SQS transcriptions │ ───────────────────▶ │ DLQ │
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

## Usage

```bash
cd environments/dev
cp terraform.tfvars.example terraform.tfvars    # edit clients; keep secrets out
export TF_VAR_keycloak_config='{"token_url":"...","client_id":"...","client_secret":"..."}'
terraform init
terraform plan
terraform apply
```

## Adding a new endpoint (e.g. banco_bogota)

1. Add an entry to the `clients` map in `terraform.tfvars`:
   ```hcl
   banco_bogota = {
     api_base_url  = "https://.../transcription/api/tipificaciones"
     endpoint_path = "b_bog"
     enabled       = true
   }
   ```
2. `terraform apply` — creates `/nexa/empatia/dev/clients/banco_bogota` and extends
   the EventBridge rule to route the `banco_bogota/` prefix.
3. Providers start dropping files under `banco_bogota/...`. No Lambda change or redeploy.

## Parameter Store layout (JSON values)

| Parameter | Type | Body |
| --- | --- | --- |
| `/nexa/empatia/<env>/keycloak` | SecureString | `{"token_url","client_id","client_secret"}` |
| `/nexa/empatia/<env>/clients/<client_key>` | String | `{"api_base_url","endpoint_path","enabled"}` |

## Notes

- `aws_s3_bucket_notification` is **authoritative** for the landing bucket. If that
  bucket already has notifications managed elsewhere, set
  `manage_bucket_notification = false` and enable EventBridge in that other config.
- SQS visibility timeout is set to 6× the Lambda timeout; the Lambda reports
  `ReportBatchItemFailures` so only failed records are retried / sent to the DLQ.
