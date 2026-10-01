"""Configuration commune des tests : environnement hermétique (aucun réseau)."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="syno-ia-tests-"))

os.environ.update(
    {
        "DATA_DIR": str(_TMP),
        "APP_SECRET": "secret-de-test-0123456789",
        "EMBEDDING_BACKEND": "none",
        "LLM_BACKEND": "none",
        "INDEX_ON_STARTUP": "false",
        "INDEX_INTERVAL_MINUTES": "0",
        "INDEX_ROOTS": str(_TMP / "shares"),
        "DSM_URL": "http://dsm.invalid:5000",
        "DSM_SERVICE_ACCOUNT": "",
        "DSM_SERVICE_PASSWORD": "",
    }
)


@pytest.fixture
def tmp_data_dir() -> Path:
    return _TMP


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
