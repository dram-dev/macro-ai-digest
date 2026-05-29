"""macro ingestor base — store binding + registry self-registration."""
from __future__ import annotations

from digest import db
from digest.ingest.base import IngestedItem, IngestorBase
from digest_core.ingest import discover, registered


def test_store_is_bound_to_macro_db():
    assert IngestorBase.store is db


def test_dummy_ingestor_persists_and_logs(fresh_db):
    class _SmokeIngestor(IngestorBase):
        name = "_smoke_macro"

        def fetch(self):
            return [IngestedItem(source="_smoke_macro", source_id="s1", title="hi")]

    fetched, new = _SmokeIngestor().run(run_type="manual")
    assert (fetched, new) == (1, 1)
    assert db.item_stats().get("_smoke_macro") == 1


def test_discover_registers_macro_ingestors():
    specs = discover("digest.ingest")
    # the generic + macro-specific sources all self-register
    for name in ("rss", "reddit", "edgar", "fred", "yahoo", "hn", "arxiv"):
        assert name in specs
    # base classes are not catalogued
    assert "base" not in registered()
