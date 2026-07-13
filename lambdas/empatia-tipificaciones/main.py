"""S3 -> SQS driven forwarder for call transcriptions.

A file dropped in the landing bucket (via Accenture's S3 replication) triggers
an EventBridge "Object Created" event that is buffered in SQS and delivered to
this Lambda. The object key's first path segment identifies the client
(e.g. ``banco_occ/2026/07/13/call-123.json``). Per-client routing and shared
Keycloak credentials are resolved from SSM Parameter Store, so onboarding a new
endpoint is config-only -- no code change or redeploy.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3

# Base SSM path for this API's parameters, e.g.
# /augusta-nexa-dev/empatia/transcripciones/detalle  (stack_id = augusta-nexa-dev)
SSM_BASE = os.environ.get(
    "SSM_BASE", "/augusta-nexa-dev/empatia/transcripciones/detalle"
)
# Shared Keycloak credentials live under the base; per-client configs are
# siblings named after the client key (e.g. .../detalle/banco_occ).
KEYCLOAK_PARAM = os.environ.get("KEYCLOAK_PARAM", f"{SSM_BASE}/keycloak")
CLIENTS_PREFIX = os.environ.get("CLIENTS_PREFIX", SSM_BASE)
# Fixed S3 key prefix the providers replicate into; the client key is the next
# path segment after it (e.g. <prefix>/banco_occ/2026/07/13/file.json).
LANDING_PREFIX = os.environ.get(
    "LANDING_PREFIX", "transacciones/empatia/api/transcripciones/detalle/"
)
CONFIG_TTL = int(os.environ.get("CONFIG_TTL_SECONDS", "300"))

_ssm = boto3.client("ssm")
_s3 = boto3.client("s3")

_token_cache = {"access_token": None, "expires_at": 0}
_config_cache = {}  # param name -> (parsed_value, expires_at)


def _get_json_param(name, with_decryption=False):
    """Fetch and parse a JSON SSM parameter, cached in-memory for CONFIG_TTL."""
    now = time.time()
    cached = _config_cache.get(name)
    if cached and now < cached[1]:
        return cached[0]

    resp = _ssm.get_parameter(Name=name, WithDecryption=with_decryption)
    value = json.loads(resp["Parameter"]["Value"])
    _config_cache[name] = (value, now + CONFIG_TTL)
    return value


def _get_keycloak_config():
    return _get_json_param(KEYCLOAK_PARAM, with_decryption=True)


def _get_client_config(client_key):
    return _get_json_param(f"{CLIENTS_PREFIX}/{client_key}", with_decryption=False)


def _get_access_token():
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    cfg = _get_keycloak_config()
    data = urllib.parse.urlencode(
        {
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        cfg["token_url"],
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    _token_cache["access_token"] = body["access_token"]
    _token_cache["expires_at"] = now + int(body.get("expires_in", 300)) - 30
    return _token_cache["access_token"]


def _post_transcription(client_cfg, payload, access_token):
    url = (
        client_cfg["api_base_url"].rstrip("/")
        + "/"
        + client_cfg["endpoint_path"].lstrip("/")
    )
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _client_key_from_object_key(object_key):
    """Client/endpoint key = first path segment after the fixed landing prefix.

    e.g. "transacciones/empatia/api/transcripciones/detalle/banco_occ/x.json"
    -> "banco_occ".
    """
    key = object_key
    if LANDING_PREFIX and key.startswith(LANDING_PREFIX):
        key = key[len(LANDING_PREFIX) :]
    return key.split("/", 1)[0]


def _read_s3_json(bucket, key):
    obj = _s3.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def _iter_s3_events(sqs_body):
    """Yield (bucket, key) pairs from an SQS message body.

    Supports both the EventBridge "Object Created" shape and the native S3
    notification shape, so the wiring can change without touching the Lambda.
    """
    data = json.loads(sqs_body)

    detail = data.get("detail")
    if detail and "bucket" in detail and "object" in detail:
        key = urllib.parse.unquote_plus(detail["object"]["key"])
        yield detail["bucket"]["name"], key
        return

    for record in data.get("Records", []):
        s3_event = record.get("s3")
        if s3_event:
            key = urllib.parse.unquote_plus(s3_event["object"]["key"])
            yield s3_event["bucket"]["name"], key


def _process_object(bucket, key):
    client_key = _client_key_from_object_key(key)
    client_cfg = _get_client_config(client_key)

    if not client_cfg.get("enabled", True):
        print(f"Client '{client_key}' disabled, skipping s3://{bucket}/{key}")
        return

    payload = _read_s3_json(bucket, key)
    token = _get_access_token()
    status, body = _post_transcription(client_cfg, payload, token)

    if status >= 400:
        raise RuntimeError(f"API {status} for s3://{bucket}/{key}: {body}")
    print(f"Forwarded s3://{bucket}/{key} to '{client_key}' -> {status}")


def lambda_handler(event, context):
    """SQS batch handler with partial-batch-failure reporting."""
    failures = []
    for record in event.get("Records", []):
        message_id = record.get("messageId")
        try:
            for bucket, key in _iter_s3_events(record["body"]):
                if not key.endswith(".json"):
                    print(f"Ignoring non-JSON object s3://{bucket}/{key}")
                    continue
                _process_object(bucket, key)
        except Exception as exc:  # noqa: BLE001 - surface any failure to SQS retry
            print(f"Failed message {message_id}: {exc}")
            if message_id:
                failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}
