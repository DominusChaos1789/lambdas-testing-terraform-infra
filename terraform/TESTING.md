# Testing the pipeline (S3 → EventBridge → SQS → Lambda → API)

A guide to verifying the whole chain, layer by layer, in the `augusta-nexa-dev`
(us-east-2) account. Work top-down for a full end-to-end test, or jump to a
single section to isolate which hop is broken.

Resource names (dev), derived from `stack_id = augusta-nexa-dev`:

| Piece | Name |
| --- | --- |
| Landing bucket | `augusta-nexa-dev-providers-landing` |
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

Set once for the copy/paste commands below:

```bash
export AWS_REGION=us-east-2
export BUCKET=augusta-nexa-dev-providers-landing
export PREFIX=external/transacciones/empatia/transcripciones
export QUEUE=augusta-nexa-dev-empatia-detalle-transcriptions
export DLQ=augusta-nexa-dev-empatia-detalle-transcriptions-dlq
export FUNCTION=augusta-nexa-dev-empatia-detalle-transcription-forwarder
export LOG_GROUP=/aws/lambda/$FUNCTION   # log group
```

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

## Unit tests (no AWS needed)

```bash
cd ../lambdas/empatia-tipificaciones
uv run pytest          # 45 tests, mocks all AWS + HTTP; 100% coverage
```
