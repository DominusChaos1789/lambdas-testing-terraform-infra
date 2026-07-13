import os

os.environ.setdefault("CLIENT_ID", "test-client-id")
os.environ.setdefault("CLIENT_SECRET", "test-client-secret")

import pytest  # noqa: E402

import main  # noqa: E402


@pytest.fixture(autouse=True)
def reset_token_cache():
    main._token_cache["access_token"] = None
    main._token_cache["expires_at"] = 0
    yield
    main._token_cache["access_token"] = None
    main._token_cache["expires_at"] = 0
