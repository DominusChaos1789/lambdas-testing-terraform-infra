import json
import time
import urllib.error
from io import BytesIO

import pytest

import main

KEYCLOAK_CFG = {
    "token_url": "https://login-server-staging.nexabpo.com/token",
    "client_id": "Connection.Apis.Auth",
    "client_secret": "s3cr3t",
}
API_BASE = "https://nexa-empatia-staging.nexabpo.com/transcription/api/tipificaciones"
CLIENT_CFG = {
    "api_base_url": API_BASE,
    "endpoint_path": "b_occ",
    "enabled": True,
}
TRANSCRIPTION = {"idCall": "123", "documento": "1063228193"}
FULL_KEY = (
    "transacciones/empatia/api/transcripciones/detalle/"
    "banco_occ/2026/07/13/call.json"
)


class FakeSSM:
    def __init__(self, params):
        self.params = params
        self.calls = []

    def get_parameter(self, Name, WithDecryption=False):
        self.calls.append((Name, WithDecryption))
        return {"Parameter": {"Value": self.params[Name]}}


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
        url="https://api.example.com",
        code=code,
        msg="error",
        hdrs=None,
        fp=BytesIO(body_bytes),
    )


@pytest.fixture
def ssm(monkeypatch):
    fake = FakeSSM(
        {
            f"{main.CLIENTS_PREFIX}/banco_occ": json.dumps(CLIENT_CFG),
            main.KEYCLOAK_PARAM: json.dumps(KEYCLOAK_CFG),
        }
    )
    monkeypatch.setattr(main, "_ssm", fake)
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


def test_get_json_param_caches(ssm):
    first = main._get_client_config("banco_occ")
    second = main._get_client_config("banco_occ")

    assert first == CLIENT_CFG
    assert second == CLIENT_CFG
    assert len(ssm.calls) == 1  # second call served from cache


def test_get_json_param_refetches_after_ttl(ssm, monkeypatch):
    monkeypatch.setattr(main, "CONFIG_TTL", 0)
    main._get_client_config("banco_occ")
    main._get_client_config("banco_occ")

    assert len(ssm.calls) == 2


def test_keycloak_config_requested_with_decryption(ssm):
    main._get_keycloak_config()
    assert (main.KEYCLOAK_PARAM, True) in ssm.calls


# ---------------------------------------------------------------------------
# _get_access_token
# ---------------------------------------------------------------------------


def test_get_access_token_fetches_and_caches(ssm, monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return FakeResponse(
            200, json.dumps({"access_token": "tok", "expires_in": 100}).encode()
        )

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    assert main._get_access_token() == "tok"
    assert calls[0].full_url == KEYCLOAK_CFG["token_url"]


def test_get_access_token_uses_cache(monkeypatch):
    main._token_cache["access_token"] = "cached"
    main._token_cache["expires_at"] = time.time() + 3600

    def fake_urlopen(req, timeout=None):
        raise AssertionError("should not call token endpoint when cached")

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    assert main._get_access_token() == "cached"


def test_get_access_token_default_expiry(ssm, monkeypatch):
    def fake_urlopen(req, timeout=None):
        return FakeResponse(200, json.dumps({"access_token": "tok"}).encode())

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    before = time.time()
    main._get_access_token()
    assert main._token_cache["expires_at"] >= before + 269


# ---------------------------------------------------------------------------
# _post_transcription
# ---------------------------------------------------------------------------


def test_post_transcription_success(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return FakeResponse(201, b'{"ok": true}')

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    status, body = main._post_transcription(CLIENT_CFG, TRANSCRIPTION, "tok")

    assert status == 201
    assert captured["req"].full_url == f"{API_BASE}/b_occ"
    assert captured["req"].get_header("Authorization") == "Bearer tok"


def test_post_transcription_http_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise http_error(500, b"boom")

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)
    status, body = main._post_transcription(CLIENT_CFG, TRANSCRIPTION, "tok")

    assert status == 500
    assert body == "boom"


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
# helpers
# ---------------------------------------------------------------------------


def test_client_key_from_object_key_strips_landing_prefix():
    assert main._client_key_from_object_key(FULL_KEY) == "banco_occ"


def test_client_key_from_object_key_without_prefix():
    assert main._client_key_from_object_key("banco_occ/2026/x.json") == "banco_occ"


def test_read_s3_json(s3):
    assert main._read_s3_json("bucket", FULL_KEY) == TRANSCRIPTION


def test_read_s3_json_passes_expected_bucket_owner(s3):
    main._read_s3_json("bucket", FULL_KEY)
    assert s3.last_kwargs["ExpectedBucketOwner"] == main.AWS_ACCOUNT_ID
    assert main.AWS_ACCOUNT_ID  # non-empty in the test environment


def test_iter_s3_events_eventbridge():
    events = list(main._iter_s3_events(eventbridge_body(key="banco_occ/a%20b.json")))
    assert events == [("bucket", "banco_occ/a b.json")]


def test_iter_s3_events_native_notification():
    body = json.dumps(
        {
            "Records": [
                {"s3": {"bucket": {"name": "b"}, "object": {"key": "banco_occ/x.json"}}}
            ]
        }
    )
    assert list(main._iter_s3_events(body)) == [("b", "banco_occ/x.json")]


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

    main._process_object("bucket", "banco_occ/2026/07/13/call.json")
    assert called == []  # short-circuits before reading the object


def test_process_object_raises_on_api_error(monkeypatch, ssm, s3):
    monkeypatch.setattr(main, "_get_access_token", lambda: "tok")
    monkeypatch.setattr(main, "_post_transcription", lambda *a: (502, "bad gateway"))

    with pytest.raises(RuntimeError, match="502"):
        main._process_object("bucket", FULL_KEY)


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------


def test_handler_success(monkeypatch, ssm, s3):
    monkeypatch.setattr(main, "_get_access_token", lambda: "tok")
    monkeypatch.setattr(main, "_post_transcription", lambda *a: (200, "{}"))

    event = {"Records": [{"messageId": "m1", "body": eventbridge_body()}]}
    result = main.lambda_handler(event, None)

    assert result == {"batchItemFailures": []}


def test_handler_skips_non_json(monkeypatch, ssm, s3):
    calls = []
    monkeypatch.setattr(main, "_process_object", lambda *a: calls.append(a))

    body = eventbridge_body(key="banco_occ/2026/07/13/note.txt")
    event = {"Records": [{"messageId": "m1", "body": body}]}
    result = main.lambda_handler(event, None)

    assert result == {"batchItemFailures": []}
    assert calls == []


def test_handler_reports_partial_failure(monkeypatch):
    def boom(bucket, key):
        raise RuntimeError("API 500")

    monkeypatch.setattr(main, "_process_object", boom)

    event = {
        "Records": [
            {"messageId": "ok", "body": eventbridge_body(key="banco_occ/x.txt")},
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
    result = main.lambda_handler(event, None)

    assert result == {"batchItemFailures": []}


def test_handler_empty_event():
    assert main.lambda_handler({}, None) == {"batchItemFailures": []}
