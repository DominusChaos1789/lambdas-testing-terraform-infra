"""S3 -> SQS driven forwarder for call transcriptions.

A file dropped in the landing bucket (via the provider's S3 replication) triggers
an EventBridge "Object Created" event that is buffered in SQS and delivered to
this Lambda. The path segment after the landing prefix identifies the client
(e.g. ``.../transcripciones/BDO/year=2026/month=07/day=13/call.json`` -> "BDO").
Objects land in Hive-partitioned folders, but only the client segment matters.

Configuration is split by sensitivity and is environment-relative (STACK_ID):

* SSM Parameter Store (String, JSON) at /<stack>/empatia/api/<client>-detalle,
  holding token_url/token_path, api_url/api_path, endpoint_path,
  enpoint_cypher_path, bucket_prefix, secret_name (relative) and enabled.
* Secrets Manager (JSON) at /<stack>/<secret_name>, holding client_id,
  client_secret, grant_type and (for encrypted delivery) cypher_code.

The stored object uses the provider's message structure (a ``messages`` array
plus flat metadata) and is mapped to the API body by ``_to_api_body`` before
sending. Per file the Lambda POSTs that body to endpoint_path and, when both
enpoint_cypher_path and cypher_code are configured, an AES-256-GCM-encrypted copy
to enpoint_cypher_path. Onboarding a new endpoint is config-only.
"""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import boto3
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Stack identity (e.g. augusta-nexa-dev / -stg / -pro). Drives the SSM parameter
# and Secrets Manager paths so the same config is portable across environments.
STACK_ID = os.environ.get("STACK_ID", "augusta-nexa-dev")
# Base path holding the per-client parameters: /<stack>/empatia/api
PARAM_PREFIX = os.environ.get("PARAM_PREFIX", f"/{STACK_ID}/empatia/api")
# Client key "BDO" -> parameter/secret suffix "bdo-detalle".
CLIENT_PARAM_SUFFIX = os.environ.get("CLIENT_PARAM_SUFFIX", "-detalle")
# Fixed S3 key prefix the providers replicate into; the client key is the next
# path segment after it (e.g. <prefix>/BDO/year=2026/month=07/day=13/file.json
# -> "BDO"). Data lands in Hive-partitioned folders under the client segment.
LANDING_PREFIX = os.environ.get(
    "LANDING_PREFIX", "external/datanexa/transacciones/empatia/transcripciones/"
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

    # WithDecryption=True is ignored for plain String parameters and decrypts
    # SecureString ones, so the routing config works either way (needs kms:Decrypt
    # via ssm in the role for SecureString).
    resp = _ssm.get_parameter(Name=name, WithDecryption=True)
    raw = resp["Parameter"]["Value"]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"SSM parameter {name} is not valid JSON ({exc}); "
            f"first 80 chars: {raw[:80]!r}"
        ) from exc
    _config_cache[name] = (value, now + CONFIG_TTL)
    return value


def _client_param_name(client_key):
    return f"{PARAM_PREFIX}/{client_key.lower()}{CLIENT_PARAM_SUFFIX}"


def _get_client_config(client_key):
    return _get_json_param(_client_param_name(client_key))


def _full_secret_name(relative_or_absolute):
    """Secret names in the parameter are environment-relative (no stack prefix)."""
    if relative_or_absolute.startswith("/"):
        return relative_or_absolute
    return f"/{STACK_ID}/{relative_or_absolute}"


def _get_secret_json(secret_name):
    """Fetch and parse a JSON secret, cached in-memory for CONFIG_TTL."""
    now = time.time()
    cached = _secret_cache.get(secret_name)
    if cached and now < cached[1]:
        return cached[0]

    resp = _secrets.get_secret_value(SecretId=secret_name)
    raw = resp["SecretString"]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Secret {secret_name} is not valid JSON ({exc})") from exc
    _secret_cache[secret_name] = (value, now + CONFIG_TTL)
    return value


def _join_url(host, *parts):
    """Build a URL from a host and one or more path segments."""
    url = host.rstrip("/")
    for part in parts:
        url += "/" + part.strip("/")
    return url


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
    """Encrypt the JSON payload and return base64(nonce + ciphertext + tag).

    Scheme: AES-256-GCM (authenticated), random 12-byte nonce prepended to the
    ciphertext; AESGCM.encrypt appends the 16-byte auth tag. This MUST match the
    EmpatIA decryptor -- if the API expects a different scheme, change only this
    function (the decryptor reads nonce=[:12], then AESGCM.decrypt(nonce, rest)).
    """
    key = base64.b64decode(cypher_code_b64)
    nonce = os.urandom(12)
    plaintext = json.dumps(payload).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def _get_access_token(token_url, creds, cache_key):
    """Client-credentials token, cached per secret (cache_key) until expiry."""
    now = time.time()
    cached = _token_cache.get(cache_key)
    if cached and now < cached["expires_at"]:
        return cached["access_token"]

    token_url = _validate_url(token_url)
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

    _token_cache[cache_key] = {
        "access_token": body["access_token"],
        "expires_at": now + int(body.get("expires_in", 300)) - 30,
    }
    return _token_cache[cache_key]["access_token"]


def _post_json(url, body_obj, access_token):
    url = _validate_url(url)
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
    # utf-8-sig strips a leading UTF-8 BOM (common when files are written on
    # Windows) which would otherwise break json.loads at char 0.
    raw = obj["Body"].read().decode("utf-8-sig")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"s3://{bucket}/{key} is not valid JSON ({exc}); "
            f"first 80 chars: {raw[:80]!r}"
        ) from exc


_ROLE_LABELS = {"assistant": "Agente", "user": "Cliente"}


def _messages_to_transcript(messages):
    """Render the provider's messages array as the transcripcion text.

    assistant -> "Agente", user -> "Cliente"; turns joined with blank lines.
    """
    lines = [
        f"**{_ROLE_LABELS.get(m.get('role', ''), m.get('role', ''))}:** "
        f"{m.get('content', '')}"
        for m in messages
    ]
    return "\n\n".join(lines)


def _to_api_body(source):
    """Map the stored transcription (new provider structure) to the API body.

    The provider writes the conversation as a ``messages`` array plus flat
    metadata; the API still requires the flat tipificacion body with a
    ``transcripcion`` text field, which we render from ``messages``. Adjust this
    mapping if the API field names change.
    """
    return {
        "idCall": source.get("genesys_cloud_id", ""),
        "callId": source.get("trace_id", ""),
        "documento": source.get("client_dni", ""),
        "primerNombre": source.get("client_name", ""),
        "primerApellido": source.get("client_last_name", ""),
        "tipoPersona": source.get("person_type", ""),
        "tipoDocumento": source.get("client_dni_type", ""),
        "transcripcion": _messages_to_transcript(source.get("messages", [])),
        "fechaInicio": source.get("exported_at", ""),
    }


def _iter_s3_events(sqs_body):
    """Yield (bucket, key) pairs from an SQS message body.

    Supports both the EventBridge "Object Created" shape and the native S3
    notification shape, so the wiring can change without touching the Lambda.
    """
    try:
        data = json.loads(sqs_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"SQS message body is not valid JSON ({exc}); "
            f"first 80 chars: {sqs_body[:80]!r}"
        ) from exc

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


def _forward(url, body_obj, token, bucket, key, label):
    status, body = _post_json(url, body_obj, token)
    if status >= 400:
        raise RuntimeError(f"API {status} ({label}) for s3://{bucket}/{key}: {body}")
    print(f"Forwarded ({label}) s3://{bucket}/{key} -> {status}")


def _cypher_path(client_cfg):
    # Config uses the key "enpoint_cypher_path" (sic); tolerate the fixed spelling.
    return client_cfg.get("enpoint_cypher_path") or client_cfg.get(
        "endpoint_cypher_path"
    )


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

    secret_name = _full_secret_name(client_cfg["secret_name"])
    creds = _get_secret_json(secret_name)
    body = _to_api_body(_read_s3_json(bucket, key))

    token_url = _join_url(client_cfg["token_url"], client_cfg["token_path"])
    token = _get_access_token(token_url, creds, secret_name)
    api_base = _join_url(client_cfg["api_url"], client_cfg["api_path"])

    # Plaintext delivery.
    _forward(
        _join_url(api_base, client_cfg["endpoint_path"]),
        body,
        token,
        bucket,
        key,
        "plain",
    )

    # Encrypted delivery (only when both the endpoint path and key are set).
    cypher_path = _cypher_path(client_cfg)
    cypher_code = creds.get("cypher_code")
    if cypher_path and cypher_code:
        encrypted = {"payload": _encrypt_payload(body, cypher_code)}
        _forward(
            _join_url(api_base, cypher_path), encrypted, token, bucket, key, "encrypted"
        )


def handler(event, context):
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
