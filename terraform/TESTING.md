# Testing the pipeline (S3 → EventBridge → SQS → Lambda → API)

A guide to verifying the whole chain, layer by layer, in the `augusta-nexa-dev`
(us-east-2) account. Work top-down for a full end-to-end test, or jump to a
single section to isolate which hop is broken.

Resource names (dev), derived from `stack_id = augusta-nexa-dev`:

| Piece | Name |
| --- | --- |
| Landing bucket | `augusta-nexa-dev-providers-transit` |
| Landing prefix | `external/transacciones/empatia/transcripciones/` |
| EventBridge rule | `augusta-nexa-dev-empatia-detalle-transcription-object-created` |
| SQS queue | `augusta-nexa-dev-empatia-detalle-transcriptions` |
| DLQ | `augusta-nexa-dev-empatia-detalle-transcriptions-dlq` |
| Lambda | `augusta-nexa-dev-empatia-detalle-transcription-forwarder` |
| Log group | `/aws/lambda/augusta-nexa-dev-empatia-detalle-transcription-forwarder` |
| SSM param (BDO) | `/augusta-nexa-dev/empatia/api/bdo-detalle` |
| Secret (BDO) | `/augusta-nexa-dev/empatia/api/bdo-detalle` |

```
S3 (Object Created) ─▶ EventBridge rule ─▶ SQS queue ─▶ Lambda ─▶ EmpatIA API
                                              │
                                              └─(after 5 tries)─▶ DLQ
```

Everything below is **AWS CLI v2** (plus `jq` for filtering). Which shell:

- **AWS CloudShell (recommended)** — `aws`, `jq`, `bash`, and GNU `date` are all
  preinstalled and you're already authenticated. Paste the `bash` blocks as-is.
- **bash** (Linux / WSL / Git Bash) — same, but on **macOS** replace
  `date -u -d '15 min ago'` with `date -u -v-15M`.
- **PowerShell** — use the setup + time helpers in the
  [Appendix: PowerShell](#appendix-powershell) below (variable syntax and the
  `date` expressions differ). The plain `aws …` calls are otherwise identical.

Set once for the copy/paste commands below (bash / CloudShell):

```bash
export AWS_REGION=us-east-2
export BUCKET=augusta-nexa-dev-providers-transit
export PREFIX=external/transacciones/empatia/transcripciones
export QUEUE=augusta-nexa-dev-empatia-detalle-transcriptions
export DLQ=augusta-nexa-dev-empatia-detalle-transcriptions-dlq
export FUNCTION=augusta-nexa-dev-empatia-detalle-transcription-forwarder
export LOG_GROUP=/aws/lambda/$FUNCTION   # log group
```

> PowerShell users: run the [Appendix](#appendix-powershell) setup block instead,
> then everywhere below swap `$BUCKET` → `$env:BUCKET` etc. (or use the `$BUCKET`
> PowerShell vars from the appendix).

---

## 0. Preconditions (must exist before any run succeeds)

- [ ] **SSM parameter** `/augusta-nexa-dev/empatia/api/bdo-detalle` exists and is valid JSON.
- [ ] **Secret** `/augusta-nexa-dev/empatia/api/bdo-detalle` exists with `client_id`,
      `client_secret`, `grant_type` (and `cypher_code` if encrypted delivery is on).
- [ ] The Lambda's env has `STACK_ID`, `PARAM_PREFIX`, `LANDING_PREFIX`, `AWS_ACCOUNT_ID`.

```bash
aws ssm get-parameter --name "/augusta-nexa-dev/empatia/api/bdo-detalle" \
  --query Parameter.Value --output text | jq .

aws secretsmanager get-secret-value \
  --secret-id "/augusta-nexa-dev/empatia/api/bdo-detalle" \
  --query SecretString --output text | jq 'keys'   # keys only, not values

aws lambda get-function-configuration --function-name "$FUNCTION" \
  --query 'Environment.Variables'
```

---

## 1. Fastest check — invoke the Lambda directly (skips S3/EventBridge/SQS)

Confirms the handler + config + API call work. It reads a **real** S3 object, so
upload one first.

```bash
# a) upload a payload to the exact key the sample event points at
aws s3 cp ../lambdas/empatia-tipificaciones/sample_payload.json \
  "s3://$BUCKET/$PREFIX/BDO/year=2026/month=07/day=13/call-99901110121647.json"

# b) invoke with the SQS-shaped sample event
aws lambda invoke --function-name "$FUNCTION" \
  --payload fileb://../lambdas/empatia-tipificaciones/test_event.json \
  --cli-binary-format raw-in-base64-out out.json
cat out.json    # expect: {"batchItemFailures": []}
```

Success looks like `{"batchItemFailures": []}` and a log line
`Forwarded (plain) s3://... -> 200`. Anything in `batchItemFailures` failed —
read the logs (section 6).

The other console events live in
[`lambdas/empatia-tipificaciones/test_events/`](../lambdas/empatia-tipificaciones/test_events/)
(single, batch, native-notification, ignored marker, partial-failure).

---

## 2. Test the SQS → Lambda hop (send a message to the queue)

Verifies the event-source mapping, batching and partial-batch-failure wiring.
Send an EventBridge-shaped body (what the rule delivers):

```bash
QUEUE_URL=$(aws sqs get-queue-url --queue-name "$QUEUE" --query QueueUrl --output text)

aws sqs send-message --queue-url "$QUEUE_URL" --message-body '{
  "detail-type": "Object Created",
  "source": "aws.s3",
  "detail": {
    "bucket": { "name": "'"$BUCKET"'" },
    "object": { "key": "'"$PREFIX"'/BDO/year=2026/month=07/day=13/call-99901110121647.json" }
  }
}'
```

Within a few seconds the Lambda should fire. Check it was consumed (queue drains)
and read the logs:

```bash
aws sqs get-queue-attributes --queue-url "$QUEUE_URL" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```

---

## 3. Test EventBridge → SQS (put a fake S3 event on the bus)

Verifies the rule pattern (source, detail-type, and the `<prefix>/<client>/`
key filter) and the rule → SQS target + permissions — without touching S3.

```bash
aws events put-events --entries '[{
  "Source": "aws.s3",
  "DetailType": "Object Created",
  "Detail": "{\"bucket\":{\"name\":\"'"$BUCKET"'\"},\"object\":{\"key\":\"'"$PREFIX"'/BDO/year=2026/month=07/day=13/call-eb-test.json\"}}",
  "EventBusName": "default"
}]'
```

`put-events` returns `FailedEntryCount: 0` on accept. If the rule matches, a
message lands in SQS and the Lambda runs. If **nothing** happens, the event
pattern didn't match — usually the key prefix. Inspect the rule:

```bash
aws events describe-rule --name augusta-nexa-dev-empatia-detalle-transcription-object-created \
  --query EventPattern --output text | jq .
```

> Note: the object key referenced above must exist in S3 for the Lambda to
> succeed (it still does a real GetObject). For a pattern-only check, an
> `_SUCCESS`/`.txt` key is fine — the Lambda ignores it but you still confirm the
> event routed.

---

## 4. Full end-to-end — drop a file in S3

The real production path. Requires that EventBridge notifications are enabled on
the bucket (Terraform sets `eventbridge = true`).

```bash
aws s3 cp ../lambdas/empatia-tipificaciones/sample_payload.json \
  "s3://$BUCKET/$PREFIX/BDO/year=2026/month=07/day=13/call-e2e-$(date +%s).json"
```

Then watch the logs (section 6). A single upload should produce one plaintext
POST (and one encrypted POST if `enpoint_cypher_path` + `cypher_code` are set).

> ⚠️ This creates a **real** tipificación in the EmpatIA staging API. Use test
> data and a disposable `idCall`.

Confirm EventBridge is actually enabled on the bucket:

```bash
aws s3api get-bucket-notification-configuration --bucket "$BUCKET" \
  --query EventBridgeConfiguration
```

---

## 5. Test the failure path (DLQ + retries)

Drop an object under a client folder that has **no** SSM parameter — the Lambda
raises, SQS retries `maxReceiveCount` (5) times, then moves it to the DLQ.

```bash
aws s3 cp ../lambdas/empatia-tipificaciones/sample_payload.json \
  "s3://$BUCKET/$PREFIX/UNKNOWN/year=2026/month=07/day=13/bad.json"

# after ~a minute of retries, inspect the DLQ
DLQ_URL=$(aws sqs get-queue-url --queue-name "$DLQ" --query QueueUrl --output text)
aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names ApproximateNumberOfMessages
aws sqs receive-message --queue-url "$DLQ_URL" --max-number-of-messages 1
```

DLQ depth > 0 is your single best "something failed permanently" signal — alarm
on `ApproximateNumberOfMessagesVisible`.

---

## 6. Reading the logs

```bash
# tail live
aws logs tail "$LOG_GROUP" --follow --since 10m

# or query with Logs Insights
aws logs start-query --log-group-name "$LOG_GROUP" \
  --start-time $(($(date +%s) - 3600)) --end-time $(date +%s) \
  --query-string 'fields @timestamp,@message | filter @message like /Forwarded|Failed message|Ignoring/ | sort @timestamp desc'
```

| Log line | Meaning |
| --- | --- |
| `Forwarded (plain) s3://... -> 200` | plaintext POST succeeded |
| `Forwarded (encrypted) s3://... -> 200` | encrypted POST succeeded |
| `Ignoring non-JSON object s3://...` | skipped a marker/non-`.json` file |
| `Client 'X' disabled, skipping ...` | `enabled: false` in the param |
| `Failed message <id>: API 500 ... : <body>` | the API rejected it (body logged) |
| `Failed message <id>: ...ParameterNotFound...` | no SSM param for that client |
| `Failed message <id>: ...outside bucket_prefix...` | key doesn't match the client's `bucket_prefix` |
| `Failed message <id>: ...non-allowlisted URL...` | `api_url`/`token_url` host isn't `*.nexabpo.com` |

---

## Troubleshooting quick map

| Symptom | Likely hop | Check |
| --- | --- | --- |
| Direct invoke fails, `NoSuchKey` | Lambda ↔ S3 | object exists at that exact key |
| Direct invoke fails, `AccessDenied` on GetObject | IAM / bucket owner | role has `s3:GetObject`; `AWS_ACCOUNT_ID` matches bucket owner |
| Direct invoke fails, `ParameterNotFound` | config | SSM param `/…/api/<client>-detalle` exists |
| SQS send works, S3 drop doesn't | EventBridge | `eventbridge=true` on bucket; rule pattern/prefix |
| Nothing reaches SQS from `put-events` | rule | event pattern (source/detail-type/key prefix) |
| Messages pile up in the queue | Lambda | event-source mapping enabled; function errors |
| Everything "works" but no API record | API | 2xx in logs? check EmpatIA side for the `idCall` |
| Encrypted POST rejected, plain OK | crypto | API's GCM framing must match `nonce+ct+tag` |

---

## Validators (per hop)

A checklist to confirm each hop actually did its job — run after the tests above.
`✅` is the expected result.

### S3 object shape (the payload the Lambda reads)

The provider stores the **new structure** (a `messages` array + flat metadata).
The Lambda maps it to the API body (`_to_api_body`) — the endpoint receives
identity fields plus the `messages` array. Validate a stored object:

```bash
aws s3 cp "s3://$BUCKET/$PREFIX/BDO/year=2026/month=07/day=13/call-99901110121647.json" - \
  | jq '{tenant_id, client_dni, turns, messages: (.messages | length)}'
```
✅ has a non-empty `messages` array (that is what gets forwarded).

### EventBridge rule matched

```bash
# metric for the rule over the last 15 min (needs the rule to have fired)
aws cloudwatch get-metric-statistics --namespace AWS/Events \
  --metric-name MatchedEvents --period 900 --statistics Sum \
  --dimensions Name=RuleName,Value=augusta-nexa-dev-empatia-detalle-transcription-object-created \
  --start-time "$(date -u -d '15 min ago' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)"
```
✅ `Sum >= 1`. If `FailedInvocations > 0`, the rule matched but couldn't deliver
to SQS (check the queue policy).

### SQS delivered and drained

```bash
QUEUE_URL=$(aws sqs get-queue-url --queue-name "$QUEUE" --query QueueUrl --output text)
aws sqs get-queue-attributes --queue-url "$QUEUE_URL" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```
✅ both counts return to `0` shortly after a run (the message was consumed). A
climbing `NotVisible` that never clears = the Lambda keeps erroring.

### Lambda invoked, succeeded, and posted the mapped body

```bash
# invocations vs errors over 15 min
aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Errors \
  --period 900 --statistics Sum --dimensions Name=FunctionName,Value=$FUNCTION \
  --start-time "$(date -u -d '15 min ago' +%FT%TZ)" --end-time "$(date -u +%FT%TZ)"

# confirm the forward happened
aws logs filter-log-events --log-group-name "$LOG_GROUP" --start-time "$(( ($(date +%s) - 900) * 1000 ))" \
  --filter-pattern '"Forwarded (plain)"' --query 'events[].message'
```
✅ `Errors Sum == 0` and at least one `Forwarded (plain) ... -> 200` line. The
`messages` array reached the API if the POST returned 2xx.

### DLQ empty (no permanent failures)

```bash
DLQ_URL=$(aws sqs get-queue-url --queue-name "$DLQ" --query QueueUrl --output text)
aws sqs get-queue-attributes --queue-url "$DLQ_URL" \
  --attribute-names ApproximateNumberOfMessages
```
✅ `0`. Anything > 0 is a message that failed 5× — inspect it (section 5).

### Local: the transform maps correctly (no AWS)

```bash
cd ../lambdas/empatia-tipificaciones
STACK_ID=augusta-nexa-dev AWS_ACCOUNT_ID=1 AWS_DEFAULT_REGION=us-east-2 \
  uv run python -c "import json,main; print(json.dumps(main._to_api_body(json.load(open('sample_payload.json'))), indent=2))"
```
✅ prints the API body with `messages`, `documento`, `primerNombre`, etc.

---

## Unit tests (no AWS needed)

```bash
cd ../lambdas/empatia-tipificaciones
uv run pytest          # 47 tests, mocks all AWS + HTTP; 100% coverage
```

---

## Appendix: PowerShell

Windows PowerShell equivalents for the shell-specific parts (variables, `date`
expressions, and inline JSON). The plain `aws …` calls not shown here are
identical to the bash blocks. Tested against Windows PowerShell 5.1.

### Setup (variables)

```powershell
$Region   = "us-east-2"
$Bucket   = "augusta-nexa-dev-providers-transit"
$Prefix   = "external/transacciones/empatia/transcripciones"
$Queue    = "augusta-nexa-dev-empatia-detalle-transcriptions"
$Dlq      = "augusta-nexa-dev-empatia-detalle-transcriptions-dlq"
$Function = "augusta-nexa-dev-empatia-detalle-transcription-forwarder"
$LogGroup = "/aws/lambda/$Function"
$env:AWS_DEFAULT_REGION = $Region   # so you can omit --region
```

### Time helpers (replace the bash `date` expressions)

```powershell
$now      = (Get-Date).ToUniversalTime()
$StartIso = $now.AddMinutes(-15).ToString("yyyy-MM-ddTHH:mm:ssZ")   # --start-time
$EndIso   = $now.ToString("yyyy-MM-ddTHH:mm:ssZ")                   # --end-time
$StartMs  = [DateTimeOffset]::UtcNow.AddMinutes(-15).ToUnixTimeMilliseconds()  # logs filter --start-time
```

### 1 — upload + direct invoke

```powershell
aws s3 cp ../lambdas/empatia-tipificaciones/sample_payload.json `
  "s3://$Bucket/$Prefix/BDO/year=2026/month=07/day=13/call-99901110121647.json"

aws lambda invoke --function-name $Function `
  --payload fileb://../lambdas/empatia-tipificaciones/test_event.json `
  --cli-binary-format raw-in-base64-out out.json
Get-Content out.json        # expect {"batchItemFailures": []}
```

### 2 — SQS send-message (build the body with a file, no quoting headaches)

```powershell
$QueueUrl = aws sqs get-queue-url --queue-name $Queue --query QueueUrl --output text
$key = "$Prefix/BDO/year=2026/month=07/day=13/call-99901110121647.json"
@{
  "detail-type" = "Object Created"; source = "aws.s3"
  detail = @{ bucket = @{ name = $Bucket }; object = @{ key = $key } }
} | ConvertTo-Json -Depth 6 | Set-Content -Encoding ascii msg.json
aws sqs send-message --queue-url $QueueUrl --message-body file://msg.json
```

### 3 — EventBridge put-events

```powershell
$key = "$Prefix/BDO/year=2026/month=07/day=13/call-eb-test.json"
# Detail must be a JSON *string* inside the entry:
$detail = @{ bucket = @{ name = $Bucket }; object = @{ key = $key } } | ConvertTo-Json -Compress
$entry  = @{ Source = "aws.s3"; DetailType = "Object Created"; Detail = $detail; EventBusName = "default" }
# force a one-element JSON array (PS 5.1 has no -AsArray):
"[" + ($entry | ConvertTo-Json -Depth 6 -Compress) + "]" | Set-Content -Encoding ascii entries.json
aws events put-events --entries file://entries.json
```

### 6 — logs + queries

```powershell
aws logs tail $LogGroup --follow --since 10m

$startEpoch = [DateTimeOffset]::UtcNow.AddHours(-1).ToUnixTimeSeconds()
$endEpoch   = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
aws logs start-query --log-group-name $LogGroup `
  --start-time $startEpoch --end-time $endEpoch `
  --query-string 'fields @timestamp,@message | filter @message like /Forwarded|Failed message|Ignoring/ | sort @timestamp desc'
```

### Validators (metrics + logs use the time helpers above)

```powershell
# EventBridge matched
aws cloudwatch get-metric-statistics --namespace AWS/Events --metric-name MatchedEvents `
  --period 900 --statistics Sum `
  --dimensions Name=RuleName,Value=augusta-nexa-dev-empatia-detalle-transcription-object-created `
  --start-time $StartIso --end-time $EndIso

# Lambda errors
aws cloudwatch get-metric-statistics --namespace AWS/Lambda --metric-name Errors `
  --period 900 --statistics Sum --dimensions Name=FunctionName,Value=$Function `
  --start-time $StartIso --end-time $EndIso

# forward log line
aws logs filter-log-events --log-group-name $LogGroup --start-time $StartMs `
  --filter-pattern '"Forwarded (plain)"' --query 'events[].message'
```

### jq alternative on Windows

CloudShell has `jq`. On Windows either `winget install jqlang.jq`, or use
`ConvertFrom-Json` instead of `| jq …`:

```powershell
# equivalent of: aws s3 cp ... - | jq '.messages | length'
(aws s3 cp "s3://$Bucket/$Prefix/BDO/year=2026/month=07/day=13/call-99901110121647.json" - `
  | ConvertFrom-Json).messages.Count
```

### Local validators (transform + pytest)

```powershell
cd ../lambdas/empatia-tipificaciones
$env:STACK_ID = "augusta-nexa-dev"; $env:AWS_ACCOUNT_ID = "1"; $env:AWS_DEFAULT_REGION = "us-east-2"
uv run python -c "import json,main; print(json.dumps(main._to_api_body(json.load(open('sample_payload.json'))), indent=2))"
uv run pytest
```
