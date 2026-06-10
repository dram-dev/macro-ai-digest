"""Brief note — the mobile-first daily front page (Wave 1)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from digest import brief, db


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _keep(conn, source_id: str, topic: str, score: float) -> None:
    conn.execute(
        """UPDATE items SET topic=?, triage_decision='keep',
           summary='S body.', why_it_matters='Stake for the reader.',
           confidence='high', triage_score=? WHERE source_id=?""",
        (topic, score, source_id),
    )


def _seed_items(make_item, spec: list[tuple[str, str, float]]) -> None:
    db.upsert_items(
        [make_item(source_id=sid, title=f"Title {sid}") for sid, _, _ in spec]
    )
    with db.get_conn() as conn:
        for sid, topic, score in spec:
            _keep(conn, sid, topic, score)


def test_top_picks_respects_per_topic_cap(fresh_db, make_item):
    _seed_items(make_item, [
        ("f1", "fed_markets", 0.95), ("f2", "fed_markets", 0.94),
        ("f3", "fed_markets", 0.93), ("f4", "fed_markets", 0.92),
        ("c1", "china", 0.50), ("c2", "china", 0.40),
    ])
    rows = db.items_for_publish(_today())["summarized"]
    picks = brief._top_picks(rows)
    topics = [r["topic"] for r in picks]
    assert topics.count("fed_markets") == 2     # capped despite higher scores
    assert topics.count("china") == 2
    assert len(picks) <= brief.TOP_PICKS


def test_render_brief_sections(fresh_db, make_item):
    today = _today()
    _seed_items(make_item, [("a1", "ai_capex", 0.9), ("a2", "ai_thinkers", 0.8)])
    with db.get_conn() as conn:
        ids = {
            r["source_id"]: r["id"]
            for r in conn.execute("SELECT id, source_id FROM items").fetchall()
        }

    db.upsert_connections(today, json.dumps([
        {"theme": "Capex meets eval doubt", "item_ids": list(ids.values()),
         "insight": "Two items, one tension."},
    ]))
    db.upsert_outcome(
        item_id=ids["a1"], horizon_days=7, outcome="contradicted",
        original_z=2.0, followup_z=-1.5, magnitude=3.5,
    )
    db.upsert_events([{
        "event_type": "macro", "event_date": "2099-01-01",
        "title": "CPI release", "symbol": None, "metadata_json": None,
    }])

    text, n_picks = brief.render_brief_note(today)
    assert n_picks == 2
    assert f"# ⚡ Brief — {today}" in text
    assert f"[[{today}]]" in text                       # link to the full daily
    assert "## 🎯 Top Signals" in text
    assert "https://claude.ai/new?q=" in text           # picks keep the chat link
    assert "Capex meets eval doubt" in text
    assert "## 🔁 Scoreboard" in text
    assert "❌ **contradicted** (z +2.00 → -1.50)" in text
    # events >7 days out are excluded — section absent with only the 2099 event
    assert "CPI release" not in text


def test_render_brief_empty_day(fresh_db):
    text, n_picks = brief.render_brief_note("2001-01-01")
    assert n_picks == 0
    assert "_No items kept by triage on this date._" in text


def test_write_brief_note_filename_avoids_daily_collision(
    fresh_db, make_item, monkeypatch, tmp_path
):
    vault = tmp_path / "vault"
    (vault / "80 Digest").mkdir(parents=True)
    monkeypatch.setattr(db.settings, "obsidian_vault_path", str(vault))
    monkeypatch.setattr(db.settings, "obsidian_digest_dir", "80 Digest")
    _seed_items(make_item, [("b1", "china", 0.7)])

    from digest.obsidian import Paths
    paths = Paths.resolve()
    paths.ensure()
    today = _today()
    target, n_picks = brief.write_brief_note(today, paths)
    assert target == paths.brief_dir / f"{today} Brief.md"
    assert target.exists()
    assert n_picks == 1
