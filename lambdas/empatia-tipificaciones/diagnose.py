#!/usr/bin/env python
"""Local diagnostic for one S3 object: runs the REAL pipeline steps against AWS
(S3 read, SSM param, Secrets, transform, URL checks) and stops at the first
failure with a full traceback. It does NOT call the EmpatIA API, so it has no
side effects.

Usage (PowerShell), from lambdas/empatia-tipificaciones:

    $env:STACK_ID="augusta-nexa-dev"
    $env:PARAM_PREFIX="/augusta-nexa-dev/empatia/api"
    $env:LANDING_PREFIX="external/datanexa/transacciones/empatia/transcripciones/"
    $env:AWS_ACCOUNT_ID="575108921774"
    $env:AWS_DEFAULT_REGION="us-east-2"
    $env:AWS_PROFILE="jdbarriosh-aws-dev"
    uv run python diagnose.py <bucket> <key>

e.g. <bucket> = augusta-nexa-dev-providers-landing
     <key>    = external/datanexa/transacciones/empatia/transcripciones/BDO/...call.json
"""

import sys
import traceback

import main


def step(label, fn):
    try:
        value = fn()
        print(f"[OK]   {label}")
        return value
    except Exception as exc:  # noqa: BLE001 - diagnostic
        print(f"[FAIL] {label}: {type(exc).__name__}: {exc}\n")
        traceback.print_exc()
        sys.exit(1)


def run(bucket, key):
    print(f"bucket = {bucket}")
    print(f"key    = {key}\n")

    client_key = main._client_key_from_object_key(key)
    print(f"client_key           = {client_key!r}")
    print(f"expected param name  = {main._client_param_name(client_key)}\n")

    cfg = step("SSM get client config", lambda: main._get_client_config(client_key))
    print(f"       param keys = {list(cfg)}")

    secret_name = main._full_secret_name(cfg["secret_name"])
    print(f"       secret name = {secret_name}")
    creds = step("Secrets get secret", lambda: main._get_secret_json(secret_name))
    print(f"       secret keys = {list(creds)}")

    def raw_s3():
        obj = main._s3.get_object(
            Bucket=bucket, Key=key, ExpectedBucketOwner=main.AWS_ACCOUNT_ID
        )
        data = obj["Body"].read()
        print(f"       size = {len(data)} bytes ; first bytes = {data[:8]!r}")
        return data

    step("S3 get_object (raw bytes)", raw_s3)
    src = step("S3 read + JSON parse", lambda: main._read_s3_json(bucket, key))
    print(f"       source keys = {list(src)}")

    body = step("transform _to_api_body", lambda: main._to_api_body(src))
    print(f"       body keys = {list(body)}")
    print(f"       messages  = {len(body.get('messages', []))}")

    token_url = main._join_url(cfg["token_url"], cfg["token_path"])
    api_base = main._join_url(cfg["api_url"], cfg["api_path"])
    plain_url = main._join_url(api_base, cfg["endpoint_path"])
    print(f"\n       token url = {token_url}")
    print(f"       plain url = {plain_url}")
    step("validate token url", lambda: main._validate_url(token_url))
    step("validate plain url", lambda: main._validate_url(plain_url))

    print("\nAll steps OK. The API was NOT called. If the Lambda still fails, the")
    print("problem is the token fetch or the POST -- check the CloudWatch log line.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python diagnose.py <bucket> <key>")
        sys.exit(2)
    run(sys.argv[1], sys.argv[2])
