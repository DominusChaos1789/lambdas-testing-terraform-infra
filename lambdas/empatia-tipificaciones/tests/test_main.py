import json
import time
import urllib.error
from io import BytesIO

import pytest

import main

API_BASE = "https://nexa-empatia-staging.nexabpo.com/transcription/api/tipificaciones"
TOKEN_URL = "https://login-server-staging.nexabpo.com/auth/realms/nexa/token"
SECRET_NAME = "/augusta-nexa-dev/empatia/api/bdo_detalle"
# S3 folder is "BDO"; it maps (via SSM) to the "banco_occ" endpoint.
# Objects land in Hive-partitioned folders under an "external/" root.
LANDING = "external/transacciones/empatia/transcripciones/detalle/"
FULL_KEY = f"{LANDING}BDO/year=2026/month=07/day=13/call.json"

CLIENT_CFG = {
    "token_url": TOKEN_URL,
    "api_base_url": API_BASE,
    "endpoint_path": "banco_occ",
    "bucket_prefix": f"{LANDING}BDO/",
    "secret_name": SECRET_NAME,
    "enabled": True,
}
SECRET_CFG = {
    "client_id": "Connection.Apis.Auth",
    "client_secret": "s3cr3t",
    "grant_type": "client_credentials",
}
TRANSCRIPTION = {"idCall": "123", "documento": "1063228193"}


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


@pytest.fixture
def ssm(monkeypatch):
    fake = FakeSSM({f"{main.CLIENTS_PREFIX}/BDO": json.dumps(CLIENT_CFG)})
    monkeypatch.setattr(main, "_ssm", fake)
    return fake


@pytest.fixture
def secrets(monkeypatch):
    fake = FakeSecrets({SECRET_NAME: json.dumps(SECRET_CFG)})
    monkeypatch.setattr(main, "_secrets", fake)
    return fake


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3({("bucket", FULL_KEY): json.dumps(TRANSCRIPTION).encode()})
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
# SSM parameter caching
# ---------------------------------------------------------------------------


def test_get_client_config_caches(ssm):
    assert main._get_client_config("BDO") == CLIENT_CFG
    assert main._get_client_config("BDO") == CLIENT_CFG
    assert len(ssm.calls) == 1  # second call served from cache


def test_get_client_config_refetches_after_ttl(ssm, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_TTL", 0)
    main._get_client_config("BDO")
    main._get_client_config("BDO")
    assert len(ssm.calls) == 2


# ---------------------------------------------------------------------------
# Secrets Manager
# ---------------------------------------------------------------------------


def test_get_secret_json_caches(secrets):
    assert main._get_secret_json(SECRET_NAME) == SECRET_CFG
    assert main._get_secret_json(SECRET_NAME) == SECRET_CFG
    assert secrets.calls == [SECRET_NAME]  # cached on the second call


def test_get_secret_json_refetches_after_ttl(secrets, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_TTL", 0)
    main._get_secret_json(SECRET_NAME)
    main._get_secret_json(SECRET_NAME)
    assert len(secrets.calls) == 2


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
        "http://nexa-empatia-staging.nexabpo.com/api",  # not https
        "https://evil.example.com/api",  # host not allowlisted
        "file:///etc/passwd",  # non-http scheme, no host
        "https://nexabpo.com.attacker.com/api",  # suffix spoof
    ],
)
def test_validate_url_rejects(url):
    with pytest.raises(ValueError, match="non-allowlisted"):
        main._validate_url(url)


# ---------------------------------------------------------------------------
# _get_access_token
# ---------------------------------------------------------------------------


def test_get_access_token_uses_secret_credentials(secrets, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        captured["data"] = req.data.decode("utf-8")
        return FakeResponse(
            200, json.dumps({"access_token": "tok", "expires_in": 100}).encode()
        )

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    assert main._get_access_token(CLIENT_CFG) == "tok"
    assert captured["req"].full_url == TOKEN_URL
    assert "client_id=Connection.Apis.Auth" in captured["data"]
    assert "grant_type=client_credentials" in captured["data"]


def test_get_access_token_cached_per_secret(secrets, monkeypatch):
    main._token_cache[SECRET_NAME] = {
        "access_token": "cached",
        "expires_at": time.time() + 3600,
    }

    def fake_urlopen(req, timeout=None):
        raise AssertionError("should not call token endpoint when cached")

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    assert main._get_access_token(CLIENT_CFG) == "cached"


def test_get_access_token_separate_cache_per_client(secrets, monkeypatch):
    """A cached token for one client must not be reused by another."""
    main._token_cache[SECRET_NAME] = {
        "access_token": "bdo-token",
        "expires_at": time.time() + 3600,
    }
    other_secret = "/augusta-nexa-dev/empatia/api/bdb_detalle"
    secrets.secrets[other_secret] = json.dumps(SECRET_CFG)
    other_cfg = {**CLIENT_CFG, "secret_name": other_secret}

    def fake_urlopen(req, timeout=None):
        return FakeResponse(200, json.dumps({"access_token": "bdb-token"}).encode())

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    assert main._get_access_token(other_cfg) == "bdb-token"
    assert main._get_access_token(CLIENT_CFG) == "bdo-token"


def test_get_access_token_default_expiry(secrets, monkeypatch):
    def fake_urlopen(req, timeout=None):
        return FakeResponse(200, json.dumps({"access_token": "tok"}).encode())

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    before = time.time()
    main._get_access_token(CLIENT_CFG)
    assert main._token_cache[SECRET_NAME]["expires_at"] >= before + 269


def test_get_access_token_defaults_grant_type(secrets, monkeypatch):
    secrets.secrets[SECRET_NAME] = json.dumps(
        {"client_id": "id", "client_secret": "s"}  # no grant_type
    )
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["data"] = req.data.decode("utf-8")
        return FakeResponse(200, json.dumps({"access_token": "tok"}).encode())

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    main._get_access_token(CLIENT_CFG)
    assert "grant_type=client_credentials" in captured["data"]


# ---------------------------------------------------------------------------
# _post_transcription
# ---------------------------------------------------------------------------


def test_post_transcription_success(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return FakeResponse(201, b'{"ok": true}')

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    status, _ = main._post_transcription(CLIENT_CFG, TRANSCRIPTION, "tok")

    assert status == 201
    assert captured["req"].full_url == f"{API_BASE}/banco_occ"
    assert captured["req"].get_header("Authorization") == "Bearer tok"


def test_post_transcription_http_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise http_error(500, b"boom")

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    status, body = main._post_transcription(CLIENT_CFG, TRANSCRIPTION, "tok")

    assert status == 500
    assert body == "boom"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_client_key_from_object_key_strips_landing_prefix():
    assert main._client_key_from_object_key(FULL_KEY) == "BDO"


def test_client_key_from_object_key_without_prefix():
    assert main._client_key_from_object_key("BDO/2026/x.json") == "BDO"


def test_read_s3_json(s3):
    assert main._read_s3_json("bucket", FULL_KEY) == TRANSCRIPTION


def test_read_s3_json_passes_expected_bucket_owner(s3):
    main._read_s3_json("bucket", FULL_KEY)
    assert s3.last_kwargs["ExpectedBucketOwner"] == main.AWS_ACCOUNT_ID
    assert main.AWS_ACCOUNT_ID  # non-empty in the test environment


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
    assert called == []  # short-circuits before reading the object


def test_process_object_rejects_key_outside_bucket_prefix(ssm, s3):
    # Resolves to client "BDO" but sits outside BDO's configured bucket_prefix
    # (it is not under the landing prefix at all).
    stray = "BDO/2026/07/13/call.json"
    with pytest.raises(RuntimeError, match="outside bucket_prefix"):
        main._process_object("bucket", stray)


def test_process_object_allows_missing_bucket_prefix(monkeypatch, s3):
    cfg = {k: v for k, v in CLIENT_CFG.items() if k != "bucket_prefix"}
    monkeypatch.setattr(main, "_get_client_config", lambda key: cfg)
    monkeypatch.setattr(main, "_get_access_token", lambda c: "tok")
    monkeypatch.setattr(main, "_post_transcription", lambda *a: (200, "{}"))

    main._process_object("bucket", FULL_KEY)  # no exception


def test_process_object_raises_on_api_error(monkeypatch, ssm, secrets, s3):
    monkeypatch.setattr(main, "_get_access_token", lambda c: "tok")
    monkeypatch.setattr(main, "_post_transcription", lambda *a: (502, "bad gateway"))

    with pytest.raises(RuntimeError, match="502"):
        main._process_object("bucket", FULL_KEY)


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------


def test_handler_success(monkeypatch, ssm, secrets, s3):
    monkeypatch.setattr(main, "_get_access_token", lambda c: "tok")
    monkeypatch.setattr(main, "_post_transcription", lambda *a: (200, "{}"))

    event = {"Records": [{"messageId": "m1", "body": eventbridge_body()}]}
    assert main.lambda_handler(event, None) == {"batchItemFailures": []}


def test_handler_skips_non_json(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_process_object", lambda *a: calls.append(a))

    body = eventbridge_body(key=f"{LANDING}BDO/2026/07/13/note.txt")
    event = {"Records": [{"messageId": "m1", "body": body}]}

    assert main.lambda_handler(event, None) == {"batchItemFailures": []}
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
    result = main.lambda_handler(event, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


def test_handler_failure_without_message_id_is_not_reported(monkeypatch):
    def boom(bucket, key):
        raise RuntimeError("API 500")

    monkeypatch.setattr(main, "_process_object", boom)

    event = {"Records": [{"body": eventbridge_body()}]}
    assert main.lambda_handler(event, None) == {"batchItemFailures": []}


def test_handler_empty_event():
    assert main.lambda_handler({}, None) == {"batchItemFailures": []}
