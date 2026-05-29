"""Enabler 0 — macro's Databricks fan-out (cross-domain lakehouse).

Hermetic: the real sink singleton is disabled in the test env (no DATABRICKS_
ENABLED), so writes are no-ops and never connect. We monkeypatch the sink's
write_* methods to capture the fan-out calls + assert their shape, independent
of enablement.
"""
from __future__ import annotations

import pytest

from digest import db
from digest_core.sinks.databricks import item_hash


@pytest.fixture
def capture_sink(monkeypatch):
    """Capture sink.write_* calls as {method: [args...]}."""
    calls: dict[str, list] = {}

    for name in ("write_ingested", "write_telemetry", "write_triage",
                 "write_summary"):
        def _make(n):
            def _rec(*args, **kwargs):
                calls.setdefault(n, []).append((args, kwargs))
            return _rec
        monkeypatch.setattr(db.sink, name, _make(name))
    return calls


def test_sink_disabled_in_test_env():
    # Hermetic guarantee: nothing reaches Databricks during tests.
    assert db.sink._enabled is False
    assert db.sink._schema_prefix == "macro_"


def test_upsert_items_fans_out_to_bronze(fresh_db, make_item, capture_sink):
    db.upsert_items([make_item(source="rss", source_id="r1")])
    assert "write_ingested" in capture_sink
    (args, _) = capture_sink["write_ingested"][0]
    items = list(args[0])
    assert items[0].source_id == "r1"


def test_log_run_fans_out_telemetry(fresh_db, capture_sink):
    db.log_run(run_type="manual", source="rss", items_fetched=3, items_new=2,
               duration_ms=10, status="ok")
    assert "write_telemetry" in capture_sink
    (args, _) = capture_sink["write_telemetry"][0]
    row = args[0]
    assert row["stage"] == "ingest" and row["source"] == "rss"
    assert row["items_in"] == 3 and row["items_out"] == 2 and row["errors"] == 0


def test_update_triage_fans_out_with_item_hash(fresh_db, make_item, capture_sink):
    db.upsert_items([make_item(source="rss", source_id="r1")])
    with db.get_conn() as conn:
        iid = conn.execute("SELECT id FROM items WHERE source_id='r1'").fetchone()["id"]
    db.update_triage(iid, "keep", 0.8, "fed_markets")
    (args, _) = capture_sink["write_triage"][0]
    assert args[0] == "rss" and args[1] == "r1"           # source, source_id
    assert args[2]["decision"] == "keep" and args[2]["topic"] == "fed_markets"
    # the hash the sink would derive
    assert item_hash("rss", "r1")


def test_update_summary_fans_out(fresh_db, make_item, capture_sink):
    db.upsert_items([make_item(source="rss", source_id="r1")])
    with db.get_conn() as conn:
        iid = conn.execute("SELECT id FROM items WHERE source_id='r1'").fetchone()["id"]
    db.update_summary(iid, "fed_markets", "S", "W", "high", ["a", "b"])
    (args, _) = capture_sink["write_summary"][0]
    assert args[0] == "rss" and args[2]["summary"] == "S" and args[2]["confidence"] == "high"
