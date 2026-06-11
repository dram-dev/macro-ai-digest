"""Wave 4 — topic state-of-play + signal hygiene."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from digest import db, obsidian, signals, topic_state
from digest.charts import _query_yahoo, _signed_series
from digest.velocity import _ensure_cluster_names


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ── Topic state-of-play ─────────────────────────────────────────────────

def test_run_topic_states_with_mocked_claude(fresh_db, make_item, monkeypatch):
    db.upsert_items([make_item(source_id="t1", title="Capex item")])
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE items SET topic='ai_capex', triage_decision='keep',
               summary='S', confidence='high', triage_score=0.9"""
        )
    reply = json.dumps({"states": {
        "ai_capex": {"state": "Capex is broadening beyond hyperscalers.",
                     "changed": "Ohio 10GW lease surfaced.",
                     "watch": "July earnings guides."},
        "not_a_topic": {"state": "x"},      # unknown topic ignored
    }})
    monkeypatch.setattr(topic_state, "call_claude", lambda *a, **k: reply)
    assert topic_state.run_topic_states(_today()) == 1

    row = db.get_topic_state("ai_capex")
    assert row["state"].startswith("Capex is broadening")
    assert row["watch"] == "July earnings guides."

    # archive header renders state on the main doc
    text, _ = obsidian.render_topic_archive("ai_capex")
    assert "🧭 State of play" in text
    assert "Ohio 10GW lease surfaced." in text


def test_rollover_docs_omit_state_header(fresh_db, make_item, monkeypatch):
    monkeypatch.setattr(db.settings, "obsidian_topic_archive_cap", 1)
    db.upsert_items([make_item(source_id=f"r{i}", title=f"Item {i}") for i in range(3)])
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE items SET topic='china', triage_decision='keep',
               summary='S', confidence='high', triage_score=0.5"""
        )
        conn.execute(
            "UPDATE items SET ingested_at = datetime('now', '-45 days') "
            "WHERE source_id IN ('r1', 'r2')"
        )
    db.upsert_topic_state("china", "Standing thesis.", "Moved.", "Watch.", "2026-W24")

    main, rollovers, _ = obsidian._render_topic_docs("china")
    assert "🧭 State of play" in main
    for text in rollovers.values():
        assert "🧭 State of play" not in text


# ── Signal table hygiene ────────────────────────────────────────────────

def _qitem(meta: dict, source: str = "fred", item_id: int = 1) -> dict:
    return {"id": item_id, "source": source, "title": "t", "url": None,
            "published_at": "2026-06-01", "ingested_at": "2026-06-01",
            "metadata_json": json.dumps(meta)}


def test_fred_table_skips_nan_rows():
    items = [
        (_qitem({"label": "SOFR", "z_score": float("nan"), "latest_value": 3.63}), 0.95, False),
        (_qitem({"label": "CPI", "z_score": 1.53, "latest_value": 335.4, "delta": 1.25}), 0.87, False),
    ]
    out = signals._render_fred(items)
    assert "CPI" in out and "+1.53σ" in out
    assert "SOFR" not in out and "nan" not in out


def test_insider_drip_collapses_but_keeps_outliers():
    def trade(i, value, date):
        return (
            _qitem({"ticker": "META", "owner": "Olivan Javier",
                    "role": "COO", "action": "sold", "value_usd": value},
                   source="insider", item_id=i) | {"ingested_at": date, "published_at": date},
            0.87, False,
        )
    items = [
        trade(1, 510_000, "2026-05-19"),
        trade(2, 526_000, "2026-06-03"),
        trade(3, 560_000, "2026-05-13"),
        trade(4, 111_000_000, "2026-06-04"),   # >5× median — must stay its own row
    ]
    out = signals._render_insider(items)
    assert "sold ×3 — routine drip" in out
    assert "$1,596,000 total" in out
    assert "2026-05-13 → 2026-06-03" in out
    assert "$111,000,000" in out               # outlier intact, not merged
    assert out.count("| META |") == 2          # one drip row + one outlier row


def test_insider_below_threshold_not_collapsed():
    items = [
        (_qitem({"ticker": "NVDA", "owner": "X", "role": "Dir", "action": "sold",
                 "value_usd": 100_000}, source="insider", item_id=i), 0.8, False)
        for i in range(2)
    ]
    out = signals._render_insider(items)
    assert "routine drip" not in out
    assert out.count("| NVDA |") == 2


def test_no_mermaid_blocks_in_quant_renderers():
    items = [(_qitem({"label": "CPI", "z_score": 1.5}), 0.9, False)]
    assert "mermaid" not in signals._render_fred(items)
    assert not hasattr(signals, "_chart_block")


def test_tier_file_uses_rolling_window(fresh_db):
    now = datetime.now(timezone.utc)
    fresh = {"id": 1, "source": "rss", "title": "Fresh leader", "url": None,
             "topic": "ai_capex", "confidence": "high", "summary": "S",
             "why_it_matters": None, "see_also": None, "metadata_json": None,
             "published_at": None, "ingested_at": (now.replace(microsecond=0)
             .isoformat()), "sentiment_label": None, "sentiment_score": None,
             "ensemble_consensus": None, "ensemble_dispersion": None, "cluster_id": None}
    stale = fresh | {"id": 2, "title": "Stale pinned item", "ingested_at": "2020-01-01T00:00:00+00:00"}
    text = signals._render_tier_file("high", [(fresh, 0.9), (stale, 1.0)], now)
    assert "Leaders — Rolling 90 Days" in text
    assert "Stale pinned item" not in text
    assert "Fresh leader" in text
    assert "window_qualifying: 1" in text


# ── Charts hygiene ──────────────────────────────────────────────────────

def test_yahoo_query_dedupes_tickers(fresh_db, make_item):
    today = _today()
    db.upsert_items([
        make_item(source="yahoo", source_id="y1", title="TSM a",
                  metadata={"ticker": "TSM", "pct_change": -5.3}),
        make_item(source="yahoo", source_id="y2", title="TSM b",
                  metadata={"ticker": "TSM", "pct_change": -3.1}),
        make_item(source="yahoo", source_id="y3", title="NVDA nan",
                  metadata={"ticker": "NVDA", "pct_change": float("nan")}),
    ])
    out = _query_yahoo(today)
    assert [d["label"] for d in out] == ["TSM"]
    assert out[0]["pct"] == -5.3               # kept the larger move


def test_signed_series_drops_all_zero_side():
    series = _signed_series([-3.75, -3.42], "Gained", "Declined")
    assert [s["title"] for s in series] == ["Declined"]
    both = _signed_series([1.0, -2.0], "Gained", "Declined")
    assert [s["title"] for s in both] == ["Gained", "Declined"]


# ── Velocity cluster naming ─────────────────────────────────────────────

def test_ensure_cluster_names_caches_and_falls_back(fresh_db, monkeypatch):
    clusters = [{"cluster_id": "macd, rsi, 50 day"}, {"cluster_id": "models, llm, paper"}]
    reply = json.dumps({"names": {
        "macd, rsi, 50 day": "Semi Technical Breakdown",
        "models, llm, paper": "",            # blank → rejected, raw id fallback
        "hallucinated, id": "Ignored",       # not in missing → rejected
    }})
    import digest.claude_cli as claude_cli
    monkeypatch.setattr(claude_cli, "call_claude", lambda *a, **k: reply)
    names = _ensure_cluster_names(clusters, "2026-06-01", "2026-06-14")
    assert names == {"macd, rsi, 50 day": "Semi Technical Breakdown"}
    assert db.get_cluster_names() == {"macd, rsi, 50 day": "Semi Technical Breakdown"}

    # second call: cache hit for the named one; only the unnamed goes missing,
    # and a Claude failure falls back silently
    monkeypatch.setattr(
        claude_cli, "call_claude",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
    )
    names = _ensure_cluster_names(clusters, "2026-06-01", "2026-06-14")
    assert names == {"macd, rsi, 50 day": "Semi Technical Breakdown"}


# ── Debate signal index ─────────────────────────────────────────────────

def test_debate_signal_index_numbering_matches_digest():
    from digest.debate import _signal_digest, _signal_index

    rows = [
        {"id": 10 + i, "title": f"Signal title {i}", "url": f"https://x.test/{i}",
         "topic": "ai_capex", "triage_score": 0.9, "sentiment_label": "bullish",
         "metadata_json": None}
        for i in range(3)
    ]
    digest_str = _signal_digest(rows)
    index = "\n".join(_signal_index(rows))
    assert digest_str.splitlines()[0].startswith("1. ")
    assert "1. [Signal title 0](https://x.test/0)" in index
    assert "3. [Signal title 2](https://x.test/2)" in index
    assert "`#12`" in index
