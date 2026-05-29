"""Shared pytest fixtures for macro-ai-digest.

The suite runs hermetically: every test gets a throwaway SQLite file pointed at
by the digest settings, so nothing touches the real DB or any external service.
macro has no Databricks sink, so (unlike PC) there's nothing to disable.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from digest import db
from digest.ingest.base import IngestedItem


@pytest.fixture
def fresh_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the digest DB at a fresh temp file and init the schema + migrations."""
    path = tmp_path / "test_state.db"
    monkeypatch.setattr(db.settings, "db_path", path)
    db.init_db(path)
    return path


@pytest.fixture
def make_item():
    """Factory for IngestedItem rows with sensible defaults."""
    def _make(
        source: str = "rss",
        source_id: str = "a1",
        title: str = "A title",
        *,
        url: str | None = "https://example.test/x",
        author: str | None = None,
        content: str | None = "body",
        published_at: datetime | None = None,
        metadata: dict | None = None,
    ) -> IngestedItem:
        return IngestedItem(
            source=source,
            source_id=source_id,
            title=title,
            url=url,
            author=author,
            content=content,
            published_at=published_at,
            metadata=metadata or {},
        )

    return _make
