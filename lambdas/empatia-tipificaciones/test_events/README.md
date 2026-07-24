# Lambda console test events

SQS-wrapped events for testing the forwarder in the AWS Lambda console
(Test tab → paste the file contents). Keys use the current layout:

```
s3://augusta-nexa-dev-providers-landing/
    external/transacciones/empatia/transcripciones/BDO/year=2026/month=07/day=13/<file>.json
```

Each event only carries an S3 **pointer** — the Lambda reads the object from S3,
maps it to the API body (the stored object uses the provider's `messages`
structure; see `_to_api_body`), and POSTs it. So the referenced object must exist
in the bucket (and the `BDO` SSM param + secret must be configured) for a
successful run. Upload [`../sample_payload.json`](../sample_payload.json) (new
structure) as the object.

| File | Scenario | Expected result |
| --- | --- | --- |
| `01_bdo_single.json` | One BDO object in a Hive partition | Forwarded; `{"batchItemFailures": []}` |
| `02_bdo_batch.json` | 3 BDO objects across day partitions | All forwarded; no failures |
| `03_native_notification.json` | Native S3 notification shape; Hive `=` URL-encoded (`%3D`) | Decoded to `BDO` and forwarded |
| `04_non_json_ignored.json` | Spark/Hive `_SUCCESS` marker (not `.json`) | Ignored; no S3 read, no failures |
| `05_mixed_partial_failure.json` | One valid BDO + one unknown client folder | BDO forwarded; the unknown record returned in `batchItemFailures` |

Notes:
- `04` proves marker files written alongside Hive data are skipped safely.
- `05`'s `UNKNOWN` record fails because there is no SSM param for that client;
  it lands in `batchItemFailures` so only that message is retried / DLQ'd, while
  the good record is deleted from the queue.
- If you want a run to actually succeed, first upload a matching object, e.g.:
  ```bash
  aws s3 cp sample_payload.json \
    "s3://augusta-nexa-dev-providers-landing/external/transacciones/empatia/transcripciones/BDO/year=2026/month=07/day=13/call-99901110121647.json"
  ```
