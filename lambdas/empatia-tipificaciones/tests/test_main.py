import json
import time
import urllib.error
from io import BytesIO

import main

VALID_PAYLOAD = {
    "idCall": "99901110121647",
    "callId": "10000033",
    "documento": "1063228193",
    "primerNombre": "NICOLASS",
    "primerApellido": "HERRERA",
    "tipoPersona": "Natural",
    "tipoDocumento": "CC",
    "transcripcion": "hola",
    "fechaInicio": "08/07/2026 10:37:00",
}


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


def json_response(status, payload):
    return FakeResponse(status, json.dumps(payload).encode("utf-8"))


def http_error(code, body_bytes):
    return urllib.error.HTTPError(
        url="https://example.com",
        code=code,
        msg="error",
        hdrs=None,
        fp=BytesIO(body_bytes),
    )


# ---------------------------------------------------------------------------
# _get_access_token
# ---------------------------------------------------------------------------


def test_get_access_token_fetches_and_caches(monkeypatch):
    requests_made = []

    def fake_urlopen(req, timeout=None):
        requests_made.append(req)
        return json_response(200, {"access_token": "abc123", "expires_in": 100})

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    token = main._get_access_token()

    assert token == "abc123"
    assert len(requests_made) == 1
    assert requests_made[0].full_url == main.KEYCLOAK_TOKEN_URL
    assert main._token_cache["access_token"] == "abc123"


def test_get_access_token_uses_cache_when_valid(monkeypatch):
    main._token_cache["access_token"] = "cached-token"
    main._token_cache["expires_at"] = time.time() + 3600

    def fake_urlopen(req, timeout=None):
        raise AssertionError("urlopen should not be called when the cache is valid")

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    token = main._get_access_token()

    assert token == "cached-token"


def test_get_access_token_refetches_when_expired(monkeypatch):
    main._token_cache["access_token"] = "stale-token"
    main._token_cache["expires_at"] = time.time() - 1

    def fake_urlopen(req, timeout=None):
        return json_response(200, {"access_token": "fresh-token", "expires_in": 60})

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    token = main._get_access_token()

    assert token == "fresh-token"


def test_get_access_token_defaults_expires_in_when_absent(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return json_response(200, {"access_token": "no-expiry-token"})

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    before = time.time()
    main._get_access_token()

    # default expires_in is 300 seconds, minus the 30s safety margin
    assert main._token_cache["expires_at"] >= before + 269


# ---------------------------------------------------------------------------
# _call_empatia
# ---------------------------------------------------------------------------


def test_call_empatia_success(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["req"] = req
        return json_response(200, {"result": "ok"})

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    status, body = main._call_empatia(VALID_PAYLOAD, "token-xyz")

    assert status == 200
    assert body == {"result": "ok"}
    assert captured["req"].get_header("Authorization") == "Bearer token-xyz"
    assert captured["req"].full_url == main.EMPATIA_API_URL


def test_call_empatia_http_error_with_json_body(monkeypatch):
    error_payload = json.dumps({"error": "bad request"}).encode("utf-8")

    def fake_urlopen(req, timeout=None):
        raise http_error(400, error_payload)

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    status, body = main._call_empatia(VALID_PAYLOAD, "token-xyz")

    assert status == 400
    assert body == {"error": "bad request"}


def test_call_empatia_http_error_with_non_json_body(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise http_error(500, b"internal server error")

    monkeypatch.setattr(main.urllib.request, "urlopen", fake_urlopen)

    status, body = main._call_empatia(VALID_PAYLOAD, "token-xyz")

    assert status == 500
    assert body == {"error": "internal server error"}


# ---------------------------------------------------------------------------
# lambda_handler
# ---------------------------------------------------------------------------


def test_lambda_handler_success_with_api_gateway_string_body(monkeypatch):
    monkeypatch.setattr(main, "_get_access_token", lambda: "token-xyz")
    monkeypatch.setattr(
        main, "_call_empatia", lambda payload, token: (200, {"result": "created"})
    )

    event = {"body": json.dumps(VALID_PAYLOAD)}
    response = main.lambda_handler(event, None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"result": "created"}
    assert response["headers"]["Content-Type"] == "application/json"


def test_lambda_handler_direct_invocation_without_body_key(monkeypatch):
    monkeypatch.setattr(main, "_get_access_token", lambda: "token-xyz")
    monkeypatch.setattr(
        main, "_call_empatia", lambda payload, token: (200, {"ok": True})
    )

    response = main.lambda_handler(VALID_PAYLOAD, None)

    assert response["statusCode"] == 200


def test_lambda_handler_body_already_a_dict(monkeypatch):
    monkeypatch.setattr(main, "_get_access_token", lambda: "token-xyz")
    monkeypatch.setattr(
        main, "_call_empatia", lambda payload, token: (200, {"ok": True})
    )

    response = main.lambda_handler({"body": VALID_PAYLOAD}, None)

    assert response["statusCode"] == 200


def test_lambda_handler_missing_fields_returns_400():
    incomplete_payload = {"idCall": "1"}

    response = main.lambda_handler(incomplete_payload, None)

    assert response["statusCode"] == 400
    error = json.loads(response["body"])["error"]
    assert "callId" in error
    assert "documento" in error


def test_lambda_handler_empty_string_body_returns_400():
    response = main.lambda_handler({"body": ""}, None)

    assert response["statusCode"] == 400


def test_lambda_handler_token_error_returns_502(monkeypatch):
    def failing_get_token():
        raise RuntimeError("keycloak is down")

    monkeypatch.setattr(main, "_get_access_token", failing_get_token)

    response = main.lambda_handler({"body": json.dumps(VALID_PAYLOAD)}, None)

    assert response["statusCode"] == 502
    body = json.loads(response["body"])
    assert body["detail"] == "keycloak is down"


def test_lambda_handler_non_dict_event_returns_400():
    response = main.lambda_handler(None, None)

    assert response["statusCode"] == 400
