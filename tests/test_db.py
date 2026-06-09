"""macro db layer — verifies it delegates to digest_core but keeps macro schema."""
from __future__ import annotations

from digest import db


def test_init_creates_base_and_macro_tables(fresh_db):
    with db.get_conn() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    # base schema owned by digest_core
    assert {"items", "run_log", "summarizer_log"} <= tables
    # macro-specific migrations layered on top
    assert {"macro_regime", "daily_connections", "signal_outcomes",
            "upcoming_events", "fred_baseline"} <= tables


def test_upsert_items_dedupes(fresh_db, make_item):
    n1 = db.upsert_items([make_item(source_id="x1"), make_item(source_id="x2")])
    assert n1 == 2
    # same source/source_id → ignored
    n2 = db.upsert_items([make_item(source_id="x1")])
    assert n2 == 0
    assert db.item_stats() == {"rss": 2}


def test_log_run_and_recent_items(fresh_db, make_item):
    db.upsert_items([make_item(source_id="r1", title="T1")])
    db.log_run(
        run_type="manual", source="rss", items_fetched=1, items_new=1,
        duration_ms=5, status="ok",
    )
    rows = db.recent_items(limit=5)
    assert any(r["title"] == "T1" for r in rows)
    with db.get_conn() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM run_log").fetchone()
    assert n == 1


def test_utcnow_iso_is_core():
    from digest_core.db import helpers as core_db
    assert db.utcnow_iso is core_db.utcnow_iso


def test_items_ready_for_summary_age_out(fresh_db, make_item):
    db.upsert_items([make_item(source_id=f"r{i}") for i in range(3)])
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET triage_decision='keep'")
        # r0 is 40 days old — past the age-out; r1/r2 are fresh
        conn.execute(
            "UPDATE items SET ingested_at = datetime('now', '-40 days') "
            "WHERE source_id = 'r0'"
        )

    all_rows = db.items_ready_for_summary(max_age_days=None)
    assert len(all_rows) == 3

    fresh_rows = db.items_ready_for_summary(max_age_days=30)
    assert {r["source_id"] for r in fresh_rows} == {"r1", "r2"}

    # window-function path (per_source_cap) honors the filter too
    capped = db.items_ready_for_summary(per_source_cap=10, max_age_days=30)
    assert {r["source_id"] for r in capped} == {"r1", "r2"}
