from __future__ import annotations

import os

import pytest

from app.core.config import reset_settings_cache
from app.db.session import reset_engine_cache

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SUPABASE_URL", "https://test-project.supabase.co")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-secret")
os.environ.setdefault("UNSUBSCRIBE_BASE_URL", "https://app.test")
os.environ.setdefault("UNSUBSCRIBE_SIGNING_KEY", "test-unsubscribe-signing-key")


@pytest.fixture(autouse=True)
def clean_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SUPABASE_URL", "https://test-project.supabase.co")
    monkeypatch.setenv("SUPABASE_JWT_SECRET", "test-secret")
    # Campaign mail is never sent without a signed unsubscribe link; tests that
    # exercise the unconfigured case clear these themselves.
    monkeypatch.setenv("UNSUBSCRIBE_BASE_URL", "https://app.test")
    monkeypatch.setenv("UNSUBSCRIBE_SIGNING_KEY", "test-unsubscribe-signing-key")
    reset_settings_cache()
    reset_engine_cache()
    yield
    reset_settings_cache()
    reset_engine_cache()
