import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

KEYCLOAK_TOKEN_URL = os.environ.get(
    "KEYCLOAK_TOKEN_URL",
    "https://login-server-staging.nexabpo.com/auth/realms/nexa/protocol/"
    "openid-connect/token",
)
EMPATIA_API_URL = os.environ.get(
    "EMPATIA_API_URL",
    "https://nexa-empatia-staging.nexabpo.com/transcription/api/tipificaciones/b_occ",
)
CLIENT_ID = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]

REQUIRED_FIELDS = [
    "idCall",
    "callId",
    "documento",
    "primerNombre",
    "primerApellido",
    "tipoPersona",
    "tipoDocumento",
    "transcripcion",
    "fechaInicio",
]

_token_cache = {"access_token": None, "expires_at": 0}


def _get_access_token():
    now = time.time()
    if _token_cache["access_token"] and now < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    data = urllib.parse.urlencode(
        {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        KEYCLOAK_TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        token_body = json.loads(resp.read().decode("utf-8"))

    _token_cache["access_token"] = token_body["access_token"]
    _token_cache["expires_at"] = now + int(token_body.get("expires_in", 300)) - 30
    return _token_cache["access_token"]


def _call_empatia(payload, access_token):
    req = urllib.request.Request(
        EMPATIA_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8")
        try:
            return exc.code, json.loads(error_body or "{}")
        except json.JSONDecodeError:
            return exc.code, {"error": error_body}


def lambda_handler(event, context):
    body = event.get("body") if isinstance(event, dict) else None
    if isinstance(body, str):
        body = json.loads(body) if body else {}
    elif body is None:
        body = event if isinstance(event, dict) else {}

    missing = [field for field in REQUIRED_FIELDS if field not in body]
    if missing:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": f"Campos faltantes: {', '.join(missing)}"}),
        }

    try:
        access_token = _get_access_token()
    except Exception as exc:
        return {
            "statusCode": 502,
            "body": json.dumps(
                {"error": "No se pudo obtener el token de Keycloak", "detail": str(exc)}
            ),
        }

    status, response_body = _call_empatia(body, access_token)

    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(response_body),
    }
