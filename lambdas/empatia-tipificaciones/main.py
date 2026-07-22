"""S3 -> SQS driven forwarder for call transcriptions.

A file dropped in the landing bucket (via the provider's S3 replication) triggers
an EventBridge "Object Created" event that is buffered in SQS and delivered to
this Lambda. The path segment after the landing prefix identifies the client
(e.g. ``.../transcripciones/BDO/year=2026/month=07/day=13/call.json`` -> "BDO").
Objects land in Hive-partitioned folders, but only the client segment matters.

Per file the Lambda POSTs the transcription to the client's plaintext endpoint
(``endpoint_path``) and, when the client's secret also defines ``cypher_id`` /
``cypher_code``, an AES-256-encrypted copy to the ``cypher_id`` endpoint.

Configuration is split by sensitivity:

* SSM Parameter Store (String, JSON) -- one parameter per client, holding
  ``token_url``, ``api_base_url``, ``endpoint_path``, ``bucket_prefix`` and the
  name of the secret to use.
* Secrets Manager (JSON) -- one secret per client (e.g. ``bdo_detalle``),
  holding ``client_id``, ``client_secret``, ``grant_type`` and, for encrypted
  delivery, ``cypher_id`` and ``cypher_code`` (base64 AES-256 key).
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Base SSM path for this API's parameters, e.g.
# /augusta-nexa-dev/empatia/transcripciones/detalle  (stack_id = augusta-nexa-dev)
SSM_BASE = os.environ.get(
    "SSM_BASE", "/augusta-nexa-dev/empatia/transcripciones/detalle"
)
# Per-client config parameters are siblings named after the client key.
CLIENTS_PREFIX = os.environ.get("CLIENTS_PREFIX", SSM_BASE)
# Fixed S3 key prefix the providers replicate into; the client key is the next
# path segment after it (e.g. <prefix>/BDO/year=2026/month=07/day=13/file.json
# -> "BDO"). Data lands in Hive-partitioned folders under the client segment.
LANDING_PREFIX = os.environ.get(
    "LANDING_PREFIX", "external/transacciones/empatia/transcripciones/"
)
CONFIG_TTL = int(os.environ.get("CONFIG_TTL_SECONDS", "300"))
# Account that owns the landing bucket. Passed as ExpectedBucketOwner on every
# S3 read so a bucket deleted and re-created in another account cannot be read.
AWS_ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "")

_ssm = boto3.client("ssm")
_s3 = boto3.client("s3")
_secrets = boto3.client("secretsmanager")

_config_cache = {}  # param name -> (parsed_value, expires_at)
_secret_cache = {}  # secret name -> (parsed_value, expires_at)
_token_cache = {}  # secret name -> {"access_token": str, "expires_at": float}


def _get_json_param(name):
    """Fetch and parse a JSON SSM parameter, cached in-memory for CONFIG_TTL."""
    now = time.time()
    cached = _config_cache.get(name)
    if cached and now < cached[1]:
        return cached[0]

    resp = _ssm.get_parameter(Name=name)
    value = json.loads(resp["Parameter"]["Value"])
    _config_cache[name] = (value, now + CONFIG_TTL)
    return value


def _get_client_config(client_key):
    return _get_json_param(f"{CLIENTS_PREFIX}/{client_key}")


def _get_secret_json(secret_name):
    """Fetch and parse a JSON secret, cached in-memory for CONFIG_TTL."""
    now = time.time()
    cached = _secret_cache.get(secret_name)
    if cached and now < cached[1]:
        return cached[0]

    resp = _secrets.get_secret_value(SecretId=secret_name)
    value = json.loads(resp["SecretString"])
    _secret_cache[secret_name] = (value, now + CONFIG_TTL)
    return value


def _validate_url(url):
    """Allow only HTTPS calls to our own backends (SSRF guard).

    The request URL comes from SSM config, so a config compromise must not be
    able to redirect traffic to an arbitrary host or use a non-HTTPS scheme
    such as file:// or ftp://.
    """
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (
        host == "nexabpo.com" or host.endswith(".nexabpo.com")
    ):
        raise ValueError(f"Refusing request to non-allowlisted URL: {url}")
    return url


def _encrypt_payload(payload, cypher_code_b64):
    """Encrypt the JSON payload and return base64(iv + ciphertext).

    Scheme: AES-256-CBC, random 16-byte IV prepended to the ciphertext, PKCS7
    padding, standard base64. This MUST match the EmpatIA decryptor -- if the
    API expects AES-GCM, Fernet, or a fixed IV, change only this function.
    """
    key = base64.b64decode(cypher_code_b64)
    iv = os.urandom(16)
    plaintext = json.dumps(payload).encode("utf-8")
    padder = sym_padding.PKCS7(algorithms.AES.block_size).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode("ascii")


def _get_access_token(client_cfg, creds):
    """Client-credentials token for one client, cached per secret until expiry."""
    secret_name = client_cfg["secret_name"]
    now = time.time()
    cached = _token_cache.get(secret_name)
    if cached and now < cached["expires_at"]:
        return cached["access_token"]

    token_url = _validate_url(client_cfg["token_url"])
    data = urllib.parse.urlencode(
        {
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "grant_type": creds.get("grant_type", "client_credentials"),
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        token_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = json.loads(resp.read().decode("utf-8"))

    _token_cache[secret_name] = {
        "access_token": body["access_token"],
        "expires_at": now + int(body.get("expires_in", 300)) - 30,
    }
    return _token_cache[secret_name]["access_token"]


def _post_json(api_base_url, endpoint_suffix, body_obj, access_token):
    url = _validate_url(api_base_url.rstrip("/") + "/" + endpoint_suffix.lstrip("/"))
    req = urllib.request.Request(
        url,
        data=json.dumps(body_obj).encode("utf-8"),
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
    """Client key = first path segment after the fixed landing prefix.

    e.g. "external/.../transcripciones/BDO/year=2026/month=07/x.json" -> "BDO".
    """
    key = object_key
    if LANDING_PREFIX and key.startswith(LANDING_PREFIX):
        key = key[len(LANDING_PREFIX) :]
    return key.split("/", 1)[0]


def _read_s3_json(bucket, key):
    obj = _s3.get_object(Bucket=bucket, Key=key, ExpectedBucketOwner=AWS_ACCOUNT_ID)
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


def _forward(api_base_url, suffix, body_obj, token, bucket, key, label):
    status, body = _post_json(api_base_url, suffix, body_obj, token)
    if status >= 400:
        raise RuntimeError(f"API {status} ({label}) for s3://{bucket}/{key}: {body}")
    print(f"Forwarded ({label}) s3://{bucket}/{key} to '{suffix}' -> {status}")


def _process_object(bucket, key):
    client_key = _client_key_from_object_key(key)
    client_cfg = _get_client_config(client_key)

    if not client_cfg.get("enabled", True):
        print(f"Client '{client_key}' disabled, skipping s3://{bucket}/{key}")
        return

    expected_prefix = client_cfg.get("bucket_prefix")
    if expected_prefix and not key.startswith(expected_prefix):
        raise RuntimeError(
            f"s3://{bucket}/{key} is outside bucket_prefix "
            f"'{expected_prefix}' configured for client '{client_key}'"
        )

    creds = _get_secret_json(client_cfg["secret_name"])
    payload = _read_s3_json(bucket, key)
    token = _get_access_token(client_cfg, creds)
    api_base_url = client_cfg["api_base_url"]

    # Plaintext delivery.
    _forward(
        api_base_url, client_cfg["endpoint_path"], payload, token, bucket, key, "plain"
    )

    # Encrypted delivery (only when the client's secret defines both fields).
    cypher_id = creds.get("cypher_id")
    cypher_code = creds.get("cypher_code")
    if cypher_id and cypher_code:
        encrypted = {"payload": _encrypt_payload(payload, cypher_code)}
        _forward(api_base_url, cypher_id, encrypted, token, bucket, key, "encrypted")


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
