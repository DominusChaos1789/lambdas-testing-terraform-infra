# Console test kit (function code + cryptography layer)

Everything to stand up the forwarder **by hand in the AWS Console**, with the
`cryptography` native dependency supplied as a **Lambda layer** so the function
itself is a single `main.py`.

```
console-test/
  main.py             # the Lambda code (paste or zip-upload)     <- FUNCTION
  build_layer.py      # builds layer.zip (python/cryptography/…)   <- LAYER
  requirements.txt    # cryptography (bundled into the layer)
  test_event.json     # SQS-shaped console test event
  sample_payload.json # the S3 object body (new provider structure)
```

> `main.py` is a copy of `../empatia-tipificaciones/main.py`. If you change the
> handler, re-copy it. Function must be **Python 3.12 / x86_64** (the layer holds
> x86_64 wheels).

---

## 1. Build & publish the cryptography layer

```powershell
cd lambdas\console-test
python build_layer.py        # -> layer.zip  (python/cryptography/… , x86_64)

aws lambda publish-layer-version --layer-name empatia-cryptography `
  --zip-file fileb://layer.zip `
  --compatible-runtimes python3.12 --compatible-architectures x86_64 `
  --region us-east-2 --profile jdbarriosh-aws-dev
```

Copy the `LayerVersionArn` from the output (e.g.
`arn:aws:lambda:us-east-2:575108921774:layer:empatia-cryptography:1`).

Verify the zip before publishing (both should be `True`):
```powershell
python -c "import zipfile; n=zipfile.ZipFile('layer.zip').namelist(); print('python/cryptography:', any(x.startswith('python/cryptography/') for x in n)); print('linux .so:', any(x.endswith('x86_64-linux-gnu.so') for x in n))"
```

---

## 2. Create the function (Console)

1. **Lambda → Create function → Author from scratch**
   - Runtime **Python 3.12**, Architecture **x86_64**.
2. **Code**: either
   - paste the contents of `main.py` into a file named `main.py` in the inline
     editor, **or**
   - upload a zip of just `main.py`
     (`Compress-Archive -Path main.py -DestinationPath function.zip`).
3. **Runtime settings → Handler** = `main.handler`.
4. **Layers → Add a layer → Custom layers** → pick `empatia-cryptography` (the
   version you published), or paste its ARN.
5. **Configuration → Environment variables**:

   | Key | Value |
   | --- | --- |
   | `STACK_ID` | `augusta-nexa-dev` |
   | `PARAM_PREFIX` | `/augusta-nexa-dev/empatia/api` |
   | `LANDING_PREFIX` | `external/datanexa/transacciones/empatia/transcripciones/` |
   | `AWS_ACCOUNT_ID` | `575108921774` |

6. **Configuration → General** → timeout **30s** (token + 2 POSTs).

---

## 3. Execution-role permissions

Attach this inline policy to the function's role (least-privilege for a
direct-invoke test — SSM param, secret, S3 read, KMS decrypt). Basic Lambda
logging comes from the managed `AWSLambdaBasicExecutionRole`.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadConfig",
      "Effect": "Allow",
      "Action": "ssm:GetParameter",
      "Resource": "arn:aws:ssm:us-east-2:575108921774:parameter/augusta-nexa-dev/empatia/api/*"
    },
    {
      "Sid": "ReadSecret",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:us-east-2:575108921774:secret:/augusta-nexa-dev/empatia/api/*"
    },
    {
      "Sid": "ReadTranscriptions",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::augusta-nexa-dev-providers-landing/*"
    },
    {
      "Sid": "DecryptSecret",
      "Effect": "Allow",
      "Action": "kms:Decrypt",
      "Resource": "*",
      "Condition": { "StringEquals": { "kms:ViaService": "secretsmanager.us-east-2.amazonaws.com" } }
    }
  ]
}
```

---

## 4. Run the test

The handler reads a **real** S3 object, so upload one first:

```powershell
aws s3 cp sample_payload.json `
  "s3://augusta-nexa-dev-providers-landing/external/datanexa/transacciones/empatia/transcripciones/BDO/year=2026/month=07/day=13/call-99901110121647.json" `
  --region us-east-2 --profile jdbarriosh-aws-dev
```

Then in the console: **Test → Create new event** → paste `test_event.json` → **Test**.

- ✅ Success: response `{"batchItemFailures": []}` and a log line
  `Forwarded (plain) s3://… -> 200`.
- `{"batchItemFailures":[{"itemIdentifier":"…"}]}` → the record failed; read the
  log line. Common causes: `NoSuchKey` (object not uploaded), `ParameterNotFound`
  (SSM param missing), `AccessDenied` (role/bucket-owner), `non-allowlisted URL`
  (config host not `*.nexabpo.com`).

Preconditions (as for the Terraform deploy): the SSM parameter and the Secrets
Manager secret for `BDO` must exist —
`/augusta-nexa-dev/empatia/api/bdo-detalle`.

---

## Notes

- **Layer vs bundling**: this kit keeps the function a single file and puts the
  native dep in the layer. The Terraform path (`../empatia-tipificaciones` +
  `build.py`) instead bundles `cryptography` into the function zip — both are
  valid; don't do both at once for the same function.
- **Architecture**: the layer is x86_64; the function must also be x86_64. For
  arm64, rebuild the layer with `PLATFORM = manylinux2014_aarch64` and set the
  function to arm64.
