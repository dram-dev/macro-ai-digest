"""Orchestration resilience — the unattended-run paths most likely to fail.

Covers the summarizer cap selection (incl. the clipped/uncapped bypass), the
per-source cap actually biting across mixed sources, a downed summarizer
skipping the batch instead of crashing, per-item failures staying isolated, and
connections degrading to [] on malformed model JSON.
"""
from __future__ import annotations

from collections import Counter

import requests

from digest import connections, db, summarize


# ── per-source cap (db layer) ────────────────────────────────────────────────
def _seed_mixed_keep_items(make_item, per_source=4):
    items = (
        [make_item(source="rss", source_id=f"a{i}") for i in range(per_source)]
        + [make_item(source="hn", source_id=f"b{i}") for i in range(per_source)]
    )
    db.upsert_items(items)
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET triage_decision='keep'")


def test_per_source_cap_limits_each_source(fresh_db, make_item):
    _seed_mixed_keep_items(make_item, per_source=4)
    rows = db.items_ready_for_summary(per_source_cap=2, max_age_days=None)
    by_source = Counter(r["source"] for r in rows)
    assert by_source == Counter({"rss": 2, "hn": 2})   # cap bites each source


def test_no_per_source_cap_returns_all_sources(fresh_db, make_item):
    _seed_mixed_keep_items(make_item, per_source=4)
    rows = db.items_ready_for_summary(per_source_cap=None, max_age_days=None)
    assert len(rows) == 8                                # uncapped → everything


# ── run_summarize cap selection ──────────────────────────────────────────────
def _spy_ready(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        summarize.db, "items_ready_for_summary",
        lambda **kw: captured.update(kw) or [],         # [] → run_summarize returns early
    )
    return captured


def test_clipped_uncapped_pass_disables_both_caps(monkeypatch):
    captured = _spy_ready(monkeypatch)
    out = summarize.run_summarize(uncapped=True)
    assert out == {"ready": 0, "succeeded": 0, "failed": 0}
    assert captured["limit"] is None
    assert captured["per_source_cap"] is None            # clipped items never capped


def test_default_pass_applies_per_source_cap(monkeypatch):
    captured = _spy_ready(monkeypatch)
    monkeypatch.setattr(summarize.settings, "summarizer_max_per_run", 75)
    monkeypatch.setattr(summarize.settings, "summarizer_max_per_source", 15)
    summarize.run_summarize()
    assert captured["limit"] == 75
    assert captured["per_source_cap"] == 15


def test_explicit_limit_overrides_per_source_cap(monkeypatch):
    captured = _spy_ready(monkeypatch)
    summarize.run_summarize(limit=5)
    assert captured["limit"] == 5
    assert captured["per_source_cap"] is None


# ── summarizer down / per-item isolation ─────────────────────────────────────
def test_summarizer_down_skips_batch_without_crashing(fresh_db, make_item, monkeypatch):
    db.upsert_items([make_item(source_id="x1"), make_item(source_id="x2")])
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET triage_decision='keep'")
    monkeypatch.setattr(summarize.settings, "summarizer_backend", "mlx_local")

    def _refused(*a, **k):
        raise requests.ConnectionError("connection refused")
    monkeypatch.setattr(summarize.requests, "post", _refused)

    out = summarize.run_summarize()
    # MLX health-probe fails → batch skipped, items left for the next run (not failed)
    assert out == {"ready": 2, "succeeded": 0, "failed": 0}


def test_per_item_backend_failure_is_isolated(fresh_db, make_item, monkeypatch):
    db.upsert_items([make_item(source_id="x1"), make_item(source_id="x2")])
    with db.get_conn() as conn:
        conn.execute("UPDATE items SET triage_decision='keep'")
    monkeypatch.setattr(summarize.settings, "summarizer_backend", "haiku_api")  # skips MLX probe

    def _boom(item, regime_framing=""):
        raise summarize.BackendError("model unavailable")
    monkeypatch.setattr(summarize, "summarize_item", _boom)

    out = summarize.run_summarize()
    assert out["ready"] == 2
    assert out["failed"] == 2          # every item failed, counted, no exception escaped
    assert out["succeeded"] == 0


# ── connections degrade on malformed model output ────────────────────────────
def test_connections_malformed_json_degrades_to_empty(monkeypatch):
    rows = [
        {"id": i, "topic": "fed_markets", "title": f"t{i}", "summary": "s", "why_it_matters": "w"}
        for i in range(1, 5)                              # >= 4 so it doesn't short-circuit
    ]
    monkeypatch.setattr(connections.db, "items_for_publish", lambda d: {"summarized": rows})
    monkeypatch.setattr(connections, "call_claude", lambda *a, **k: "sorry, no JSON here")
    stored: dict = {}
    monkeypatch.setattr(connections.db, "upsert_connections",
                        lambda date, js: stored.update(date=date, threads_json=js))

    out = connections.run_connections("2026-06-21")
    assert out == []
    assert stored["threads_json"] == "[]"                # empty list persisted, no crash
