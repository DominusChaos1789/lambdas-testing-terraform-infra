import base64
import json
import time
import urllib.error
from io import BytesIO

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import main

TOKEN_HOST = "https://login-server-staging.nexabpo.com"
TOKEN_PATH = "auth/realms/nexa/protocol/openid-connect/token"
API_HOST = "https://nexa-empatia-staging.nexabpo.com"
API_PATH = "transcription/api/tipificaciones"
FULL_TOKEN_URL = f"{TOKEN_HOST}/{TOKEN_PATH}"
FULL_PLAIN_URL = f"{API_HOST}/{API_PATH}/banco_occ"
FULL_CYPHER_URL = f"{API_HOST}/{API_PATH}/bboc_encrip"

SECRET_REL = "empatia/api/bdo-detalle"
SECRET_FULL = "/augusta-nexa-dev/empatia/api/bdo-detalle"
CYPHER_KEY = "U5n1nSa2gNqye/Sfo2ZLe9jVfVljcCSKuKkFQk0tBxw="  # throwaway 32-byte AES key

LANDING = "external/transacciones/empatia/transcripciones/"
FULL_KEY = f"{LANDING}BDO/year=2026/month=07/day=13/call.json"

CLIENT_CFG = {
    "token_url": TOKEN_HOST,
    "token_path": TOKEN_PATH,
    "api_url": API_HOST,
    "api_path": API_PATH,
    "endpoint_path": "banco_occ",
    "enpoint_cypher_path": "bboc_encrip",
    "bucket_prefix": f"{LANDING}BDO/",
    "secret_name": SECRET_REL,
    "enabled": True,
}
SECRET_CFG = {
    "client_id": "Connection.Apis.Auth",
    "client_secret": "s3cr3t",
    "grant_type": "client_credentials",
}
SECRET_CFG_ENC = {**SECRET_CFG, "cypher_code": CYPHER_KEY}

# What the provider now stores in S3 (new structure with a `messages` array).
SOURCE = {
    "tenant_id": "banco_occidente",
    "client_name": "NICOLASS",
    "client_last_name": "HERRERA",
    "client_dni": "1063228193",
    "client_dni_type": "CC",
    "person_type": "Natural",
    "genesys_cloud_id": "3fd93dd5-1a36-47aa-a72b-e2a713691f7a",
    "trace_id": "b94230fb82d7c03cd6beae12b3288abd",
    "exported_at": "2026-07-23T11:08:09-05:00",
    "turns": 2,
    "messages": [
        {"role": "assistant", "content": "Hola, soy el agente virtual."},
        {"role": "user", "content": "Buenos dias, olvide mi clave."},
    ],
}
# What _to_api_body should produce for the endpoint.
API_BODY = {
    "idCall": "3fd93dd5-1a36-47aa-a72b-e2a713691f7a",
    "callId": "b94230fb82d7c03cd6beae12b3288abd",
    "documento": "1063228193",
    "primerNombre": "NICOLASS",
    "primerApellido": "HERRERA",
    "tipoPersona": "Natural",
    "tipoDocumento": "CC",
    "fechaInicio": "2026-07-23T11:08:09-05:00",
    "messages": SOURCE["messages"],
}


class FakeSSM:
    def __init__(self, params):
        self.params = params
        self.calls = []

    def get_parameter(self, Name):
        self.calls.append(Name)
        return {"Parameter": {"Value": self.params[Name]}}


class FakeSecrets:
    def __init__(self, secrets):
        self.secrets = secrets
        self.calls = []

    def get_secret_value(self, SecretId):
        self.calls.append(SecretId)
        return {"SecretString": self.secrets[SecretId]}


class FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.last_kwargs = None

    def get_object(self, Bucket, Key, **kwargs):
        self.last_kwargs = {"Bucket": Bucket, "Key": Key, **kwargs}
        return {"Body": BytesIO(self.objects[(Bucket, Key)])}


class FakeResponse:
    def __init__(self, status, payload_bytes):
        self.status = status
        self._payload_bytes = payload_bytes

    def read(self):
        return self._payload_bytes

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False


def http_error(code, body_bytes):
    return urllib.error.HTTPError(
        url="https://nexa-empatia-staging.nexabpo.com",
        code=code,
        msg="error",
        hdrs=None,
        fp=BytesIO(body_bytes),
    )


def aes_decrypt(token_b64, key_b64):
    raw = base64.b64decode(token_b64)
    nonce, ct = raw[:12], raw[12:]
    plaintext = AESGCM(base64.b64decode(key_b64)).decrypt(nonce, ct, None)
    return json.loads(plaintext.decode())


@pytest.fixture
def ssm(monkeypatch):
    fake = FakeSSM({main._client_param_name("BDO"): json.dumps(CLIENT_CFG)})
    monkeypatch.setattr(main, "_ssm", fake)
    return fake


@pytest.fixture
def secrets(monkeypatch):
    fake = FakeSecrets({SECRET_FULL: json.dumps(SECRET_CFG)})
    monkeypatch.setattr(main, "_secrets", fake)
    return fake


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3({("bucket", FULL_KEY): json.dumps(SOURCE).encode()})
    monkeypatch.setattr(main, "_s3", fake)
    return fake


def eventbridge_body(bucket="bucket", key=FULL_KEY):
    return json.dumps(
        {
            "detail-type": "Object Created",
            "source": "aws.s3",
            "detail": {"bucket": {"name": bucket}, "object": {"key": key}},
        }
    )


# ---------------------------------------------------------------------------
# naming / joins
# ---------------------------------------------------------------------------


def test_client_param_name():
    assert main._client_param_name("BDO") == "/augusta-nexa-dev/empatia/api/bdo-detalle"


def test_full_secret_name_prepends_stack_when_relative():
    assert main._full_secret_name(SECRET_REL) == SECRET_FULL


def test_full_secret_name_leaves_absolute_untouched():
    assert main._full_secret_name("/other/path") == "/other/path"


def test_join_url():
    assert main._join_url(API_HOST, "a/", "/b") == f"{API_HOST}/a/b"


# ---------------------------------------------------------------------------
# config / secret caching
# ---------------------------------------------------------------------------


def test_get_client_config_caches(ssm):
    assert main._get_client_config("BDO") == CLIENT_CFG
    assert main._get_client_config("BDO") == CLIENT_CFG
    assert len(ssm.calls) == 1


def test_get_client_config_refetches_after_ttl(ssm, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_TTL", 0)
    main._get_client_config("BDO")
    main._get_client_config("BDO")
    assert len(ssm.calls) == 2


def test_get_secret_json_caches(secrets):
    assert main._get_secret_json(SECRET_FULL) == SECRET_CFG
    assert main._get_secret_json(SECRET_FULL) == SECRET_CFG
    assert secrets.calls == [SECRET_FULL]


def test_get_secret_json_refetches_after_ttl(secrets, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_TTL", 0)
    main._get_secret_json(SECRET_FULL)
    main._get_secret_json(SECRET_FULL)
    assert len(secrets.calls) == 2


# ---------------------------------------------------------------------------
# _to_api_body
# ---------------------------------------------------------------------------


def test_to_api_body_maps_new_structure():
    assert main._to_api_body(SOURCE) == API_BODY


def test_to_api_body_defaults_missing_fields():
    body = main._to_api_body({"messages": [{"role": "user", "content": "hi"}]})
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["documento"] == ""  # missing source fields default to ""


# ---------------------------------------------------------------------------
# _encrypt_payload
# ---------------------------------------------------------------------------


def test_encrypt_payload_roundtrips():
    token = main._encrypt_payload(API_BODY, CYPHER_KEY)
    assert aes_decrypt(token, CYPHER_KEY) == API_BODY


def test_encrypt_payload_uses_random_iv():
    a = main._encrypt_payload(API_BODY, CYPHER_KEY)
    b = main._encrypt_payload(API_BODY, CYPHER_KEY)
    assert a != b
    assert aes_decrypt(a, CYPHER_KEY) == aes_decrypt(b, CYPHER_KEY)


# ---------------------------------------------------------------------------
# _validate_url (SSRF guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://nexabpo.com/token",
        "https://login-server-staging.nexabpo.com/auth/token",
        "https://nexa-empatia-staging.nexabpo.com/transcription/api",
    ],
)
def test_validate_url_allows_https_nexabpo(url):
    assert main._validate_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://nexa-empatia-staging.nexabpo.com/api",
        "https://evil.example.com/api",
        "file:///etc/passwd",
        "https://nexabpo.com.attacker.com/api",
    ],
)
def test_validate_url_rejects(url):
    with pytest.raises(ValueError, match="non-allowlisted"):
        main._validate_url(url)


# ---------------------------------------------------------------------------
# _get_access_token
# ---------------------------------------------------------------------------


def test_get_access_token_uses_secret_credentials(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["data"] = req.data.decode("utf-8")
        return FakeResponse(
            200, json.dumps({"access_token": "tok", "expires_in": 100}).encode()
        )

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    assert main._get_access_token(FULL_TOKEN_URL, SECRET_CFG, SECRET_FULL) == "tok"
    assert captured["req"].full_url == FULL_TOKEN_URL
    assert "client_id=Connection.Apis.Auth" in captured["data"]
    assert "grant_type=client_credentials" in captured["data"]


def test_get_access_token_cached_per_secret(monkeypatch):
    main._token_cache[SECRET_FULL] = {
        "access_token": "cached",
        "expires_at": time.time() + 3600,
    }

    def fake_urlopen(req, timeout=None):
        raise AssertionError("should not call token endpoint when cached")

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    assert main._get_access_token(FULL_TOKEN_URL, SECRET_CFG, SECRET_FULL) == "cached"


def test_get_access_token_separate_cache_per_client(monkeypatch):
    main._token_cache[SECRET_FULL] = {
        "access_token": "bdo-token",
        "expires_at": time.time() + 3600,
    }
    other_key = "/augusta-nexa-dev/empatia/api/bdb-detalle"

    def fake_urlopen(req, timeout=None):
        return FakeResponse(200, json.dumps({"access_token": "bdb-token"}).encode())

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    assert main._get_access_token(FULL_TOKEN_URL, SECRET_CFG, other_key) == "bdb-token"
    assert (
        main._get_access_token(FULL_TOKEN_URL, SECRET_CFG, SECRET_FULL) == "bdo-token"
    )


def test_get_access_token_default_expiry(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return FakeResponse(200, json.dumps({"access_token": "tok"}).encode())

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    before = time.time()
    main._get_access_token(FULL_TOKEN_URL, SECRET_CFG, SECRET_FULL)
    assert main._token_cache[SECRET_FULL]["expires_at"] >= before + 269


def test_get_access_token_defaults_grant_type(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data.decode("utf-8")
        return FakeResponse(200, json.dumps({"access_token": "tok"}).encode())

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    main._get_access_token(
        FULL_TOKEN_URL, {"client_id": "id", "client_secret": "s"}, "k"
    )
    assert "grant_type=client_credentials" in captured["data"]


# ---------------------------------------------------------------------------
# _post_json
# ---------------------------------------------------------------------------


def test_post_json_success(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return FakeResponse(201, b'{"ok": true}')

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    status, _ = main._post_json(FULL_PLAIN_URL, {"a": 1}, "tok")

    assert status == 201
    assert captured["req"].full_url == FULL_PLAIN_URL
    assert captured["req"].get_header("Authorization") == "Bearer tok"


def test_post_json_http_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise http_error(500, b"boom")

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    status, body = main._post_json(FULL_PLAIN_URL, {"a": 1}, "tok")

    assert status == 500
    assert body == "boom"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_client_key_from_object_key_strips_landing_prefix():
    assert main._client_key_from_object_key(FULL_KEY) == "BDO"


def test_client_key_from_object_key_without_prefix():
    assert main._client_key_from_object_key("BDO/year=2026/x.json") == "BDO"


def test_cypher_path_tolerates_correct_spelling():
    assert main._cypher_path({"endpoint_cypher_path": "x"}) == "x"
    assert main._cypher_path({}) is None


def test_read_s3_json(s3):
    assert main._read_s3_json("bucket", FULL_KEY) == SOURCE


def test_read_s3_json_passes_expected_bucket_owner(s3):
    main._read_s3_json("bucket", FULL_KEY)
    assert s3.last_kwargs["ExpectedBucketOwner"] == main.AWS_ACCOUNT_ID
    assert main.AWS_ACCOUNT_ID


def test_iter_s3_events_eventbridge():
    events = list(main._iter_s3_events(eventbridge_body(key="BDO/a%20b.json")))
    assert events == [("bucket", "BDO/a b.json")]


def test_iter_s3_events_native_notification():
    body = json.dumps(
        {
            "Records": [
                {"s3": {"bucket": {"name": "b"}, "object": {"key": "BDO/x.json"}}}
            ]
        }
    )
    assert list(main._iter_s3_events(body)) == [("b", "BDO/x.json")]


def test_iter_s3_events_native_record_without_s3_is_skipped():
    body = json.dumps({"Records": [{"eventName": "ObjectRemoved"}]})
    assert list(main._iter_s3_events(body)) == []


def test_iter_s3_events_empty():
    assert list(main._iter_s3_events(json.dumps({"foo": "bar"}))) == []


# ---------------------------------------------------------------------------
# _process_object
# ---------------------------------------------------------------------------


def test_process_object_disabled_client(monkeypatch, s3):
    monkeypatch.setattr(
        main, "_get_client_config", lambda key: {**CLIENT_CFG, "enabled": False}
    )
    called = []
    monkeypatch.setattr(main, "_read_s3_json", lambda *a: called.append(a))

    main._process_object("bucket", FULL_KEY)
    assert called == []


def test_process_object_rejects_key_outside_bucket_prefix(ssm, s3):
    stray = "BDO/year=2026/month=07/day=13/call.json"  # not under landing prefix
    with pytest.raises(RuntimeError, match="outside bucket_prefix"):
        main._process_object("bucket", stray)


def test_process_object_plain_only_when_no_cypher(monkeypatch, ssm, secrets, s3):
    monkeypatch.setattr(main, "_get_access_token", lambda url, creds, key: "tok")
    posted = []

    def fake_post(url, body, tok):
        posted.append((url, body))
        return 200, "{}"

    monkeypatch.setattr(main, "_post_json", fake_post)
    main._process_object("bucket", FULL_KEY)
    # posts the transformed body (with messages) to the plain endpoint only
    assert posted == [(FULL_PLAIN_URL, API_BODY)]


def test_process_object_plain_and_encrypted(monkeypatch, ssm, secrets, s3):
    secrets.secrets[SECRET_FULL] = json.dumps(SECRET_CFG_ENC)
    monkeypatch.setattr(main, "_get_access_token", lambda url, creds, key: "tok")
    posted = []

    def fake_post(url, body, tok):
        posted.append((url, body))
        return 200, "{}"

    monkeypatch.setattr(main, "_post_json", fake_post)
    main._process_object("bucket", FULL_KEY)

    assert [p[0] for p in posted] == [FULL_PLAIN_URL, FULL_CYPHER_URL]
    assert posted[0][1] == API_BODY
    assert set(posted[1][1]) == {"payload"}
    assert aes_decrypt(posted[1][1]["payload"], CYPHER_KEY) == API_BODY


def test_process_object_allows_missing_bucket_prefix(monkeypatch, secrets, s3):
    cfg = {k: v for k, v in CLIENT_CFG.items() if k != "bucket_prefix"}
    monkeypatch.setattr(main, "_get_client_config", lambda key: cfg)
    monkeypatch.setattr(main, "_get_access_token", lambda url, creds, key: "tok")
    monkeypatch.setattr(main, "_post_json", lambda *a: (200, "{}"))
    main._process_object("bucket", FULL_KEY)  # no exception


def test_process_object_raises_on_plain_api_error(monkeypatch, ssm, secrets, s3):
    monkeypatch.setattr(main, "_get_access_token", lambda url, creds, key: "tok")
    monkeypatch.setattr(main, "_post_json", lambda *a: (502, "bad gateway"))
    with pytest.raises(RuntimeError, match="502 .plain."):
        main._process_object("bucket", FULL_KEY)


def test_process_object_raises_on_encrypted_api_error(monkeypatch, ssm, secrets, s3):
    secrets.secrets[SECRET_FULL] = json.dumps(SECRET_CFG_ENC)
    monkeypatch.setattr(main, "_get_access_token", lambda url, creds, key: "tok")

    def fake_post(url, body, tok):
        return (200, "{}") if url == FULL_PLAIN_URL else (500, "boom")

    monkeypatch.setattr(main, "_post_json", fake_post)
    with pytest.raises(RuntimeError, match="500 .encrypted."):
        main._process_object("bucket", FULL_KEY)


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


def test_handler_success(monkeypatch, ssm, secrets, s3):
    monkeypatch.setattr(main, "_get_access_token", lambda url, creds, key: "tok")
    monkeypatch.setattr(main, "_post_json", lambda *a: (200, "{}"))
    event = {"Records": [{"messageId": "m1", "body": eventbridge_body()}]}
    assert main.handler(event, None) == {"batchItemFailures": []}


def test_handler_skips_non_json(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_process_object", lambda *a: calls.append(a))
    body = eventbridge_body(key=f"{LANDING}BDO/year=2026/_SUCCESS")
    event = {"Records": [{"messageId": "m1", "body": body}]}
    assert main.handler(event, None) == {"batchItemFailures": []}
    assert calls == []


def test_handler_reports_partial_failure(monkeypatch):
    def boom(bucket, key):
        raise RuntimeError("API 500")

    monkeypatch.setattr(main, "_process_object", boom)
    event = {
        "Records": [
            {"messageId": "ok", "body": eventbridge_body(key=f"{LANDING}BDO/x.txt")},
            {"messageId": "bad", "body": eventbridge_body()},
        ]
    }
    assert main.handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "bad"}]
    }


def test_handler_failure_without_message_id_is_not_reported(monkeypatch):
    def boom(bucket, key):
        raise RuntimeError("API 500")

    monkeypatch.setattr(main, "_process_object", boom)
    event = {"Records": [{"body": eventbridge_body()}]}
    assert main.handler(event, None) == {"batchItemFailures": []}


def test_handler_empty_event():
    assert main.handler({}, None) == {"batchItemFailures": []}
