import os

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "575108921774")
os.environ.setdefault("STACK_ID", "augusta-nexa-dev")

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
