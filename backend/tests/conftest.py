from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))
sys.path.insert(0, str(REPOSITORY_ROOT))
TEST_ROOT = Path(tempfile.mkdtemp(prefix="public-archive-tests-"))
os.environ["SEEU_ENV_FILE"] = ""
os.environ["WEBUI_LOGIN_TOKEN"] = "test-webui-login-token-long-enough-123456"
os.environ["WEBUI_PORT"] = "8080"
os.environ["SESSION_SECRET"] = "test-session-secret-that-is-longer-than-32-characters"
os.environ["DATABASE_PATH"] = str(TEST_ROOT / "state" / "app.db")
os.environ["ARCHIVE_ROOT"] = str(TEST_ROOT / "archive")
os.environ["PROVIDER_STAGING_ROOT"] = str(TEST_ROOT / "provider-staging")
os.environ["PROVIDER_BASE_URL"] = ""
os.environ["PROVIDER_API_TOKEN"] = ""
os.environ["MIN_FREE_DISK_GB"] = "0.1"
os.environ["SCHEDULER_ENABLED"] = "false"


@pytest.fixture(autouse=True)
def clean_database():
    from app.config import get_settings
    from app.database import Base, engine

    settings = get_settings()
    for root in (
        settings.archive_root,
        settings.provider_staging_root,
    ):
        shutil.rmtree(root, ignore_errors=True)
    settings.ensure_directories()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
