"""Triage window: "everything new since the previous run".

With one scheduled run a day, a fixed 24h window sits right on the cadence, so
any drift in start time strands the previous run's leftovers. The window
instead reaches back past the previous scheduled run, floored at
TRIAGE_LOOKBACK_HOURS. (The anchor math itself is tested in digest-core.)
"""
from __future__ import annotations

from digest import db, triage


def _log_run_hours_ago(run_type: str, hours: float) -> None:
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO run_log (run_at, run_type, source, items_fetched, items_new, "
            "duration_ms, status) VALUES (datetime('now', ?), ?, 'rss', 0, 0, 0, 'ok')",
            (f"-{hours * 60:.0f} minutes", run_type),
        )


def test_previous_runs_leftover_is_triaged_despite_being_older_than_24h(
    fresh_db, make_item, monkeypatch
):
    monkeypatch.setattr(db.settings, "triage_lookback_hours", 24)
    db.upsert_items([make_item(source_id="leftover", title="Leftover")])
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET ingested_at = datetime('now', '-25 hours')")
    _log_run_hours_ago("daily", 24.8)

    hours = db.triage_lookback_hours()
    assert hours == 27
    assert "Leftover" not in [r["title"] for r in db.items_needing_triage(limit=10)]
    assert "Leftover" in [
        r["title"] for r in db.items_needing_triage(limit=10, lookback_hours=hours)
    ]


def test_manual_runs_do_not_anchor_the_window(fresh_db, monkeypatch):
    monkeypatch.setattr(db.settings, "triage_lookback_hours", 24)
    _log_run_hours_ago("manual", 100)
    assert db.triage_lookback_hours() == 24


def test_run_triage_uses_the_resolved_window_and_cap(fresh_db, monkeypatch):
    monkeypatch.setattr(triage.settings, "triage_max_per_run", 7)
    seen: dict = {}

    def _needing(limit, lookback_hours=None):
        seen["limit"], seen["lookback"] = limit, lookback_hours
        return []

    monkeypatch.setattr(triage.db, "auto_keep_quantitative", lambda: 0)
    monkeypatch.setattr(triage.db, "items_needing_triage", _needing)
    monkeypatch.setattr(triage.db, "triage_lookback_hours", lambda: 31)
    triage.run_triage()
    assert seen == {"limit": 7, "lookback": 31}
