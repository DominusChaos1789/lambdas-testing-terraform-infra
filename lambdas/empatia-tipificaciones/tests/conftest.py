import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("ENV", "test")

import pytest  # noqa: E402

import main  # noqa: E402


@pytest.fixture(autouse=True)
def reset_caches():
    main._token_cache["access_token"] = None
    main._token_cache["expires_at"] = 0
    main._config_cache.clear()
    yield
    main._token_cache["access_token"] = None
    main._token_cache["expires_at"] = 0
    main._config_cache.clear()
