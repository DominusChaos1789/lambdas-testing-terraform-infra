import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "111122223333")

import pytest  # noqa: E402

import main  # noqa: E402


def _clear():
    main._token_cache.clear()
    main._config_cache.clear()
    main._secret_cache.clear()


@pytest.fixture(autouse=True)
def reset_caches():
    _clear()
    yield
    _clear()
