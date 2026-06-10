"""macro obsidian — confirms the lift onto digest_core render/paths/archive."""
from __future__ import annotations

import pytest

from digest import db, obsidian
from digest_core.obsidian.archive import INDEX_BEGIN
from digest_core.obsidian.paths import Paths as CorePaths


def test_paths_is_core_subclass_and_resolves(monkeypatch, tmp_path):
    assert issubclass(obsidian.Paths, CorePaths)
    vault = tmp_path / "vault"
    (vault / "80 Digest").mkdir(parents=True)
    monkeypatch.setattr(db.settings, "obsidian_vault_path", str(vault))
    monkeypatch.setattr(db.settings, "obsidian_digest_dir", "80 Digest")
    p = obsidian.Paths.resolve()
    assert isinstance(p, obsidian.Paths)        # subclass-safe for_vault
    assert p.daily_dir == vault / "80 Digest" / "Daily"


def test_paths_resolve_raises_when_vault_unset(monkeypatch):
    monkeypatch.setattr(db.settings, "obsidian_vault_path", "")
    with pytest.raises(RuntimeError, match="OBSIDIAN_VAULT_PATH is not set"):
        obsidian.Paths.resolve()


def test_wikilink_resolves_macro_label():
    assert obsidian._wikilink("fed_markets") == "[[Fed & Markets]]"


def test_chat_link_uses_macro_framing(make_item):
    # _chat_link reads a row; a dict is row-compatible for the keys it touches.
    row = {"id": 7, "title": "T", "url": "u", "source": "rss",
           "author": None, "published_at": None, "summary": None,
           "why_it_matters": None}
    link = obsidian._chat_link(row)
    assert link.startswith("[#7](https://claude.ai/new?q=")
    # macro framing is URL-encoded in the prompt
    assert "macro%2FAI%20digest" in link


def test_render_topic_archive_uses_core_index_and_markers(fresh_db, make_item):
    db.upsert_items([make_item(source="rss", source_id="t1", title="Fed holds rates")])
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE items SET topic='fed_markets', triage_decision='keep',
               summary='The Fed held rates steady.', why_it_matters='Rate path.',
               confidence='high', triage_score=0.8 WHERE source_id='t1'"""
        )

    text, item_ids = obsidian.render_topic_archive("fed_markets")
    assert len(item_ids) == 1
    assert INDEX_BEGIN in text                       # core-built index block
    assert obsidian.ITEM_BEGIN.format(id=item_ids[0]) in text
    # link diet: archives carry a plain id ref, not the URL-encoded chat link
    assert f"`#{item_ids[0]}`" in text
    assert "https://claude.ai/new?q=" not in text
    assert "Fed holds rates" in text


def test_topic_archive_cap_rolls_over_by_month(fresh_db, make_item, monkeypatch, tmp_path):
    monkeypatch.setattr(db.settings, "obsidian_topic_archive_cap", 2)
    vault = tmp_path / "vault"
    (vault / "80 Digest").mkdir(parents=True)
    monkeypatch.setattr(db.settings, "obsidian_vault_path", str(vault))
    monkeypatch.setattr(db.settings, "obsidian_digest_dir", "80 Digest")

    db.upsert_items([make_item(source_id=f"c{i}", title=f"Item {i}") for i in range(4)])
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE items SET topic='china', triage_decision='keep',
               summary='S', confidence='high', triage_score=0.5"""
        )
        # c0/c1 newest (this month); c2/c3 older, in a different month
        conn.execute(
            "UPDATE items SET ingested_at = datetime('now', '-45 days') "
            "WHERE source_id IN ('c2', 'c3')"
        )

    paths = obsidian.Paths.resolve()
    paths.ensure()
    target, count = obsidian.write_topic_archive("china", paths)
    assert count == 4

    main = target.read_text(encoding="utf-8")
    assert "Item 0" in main and "Item 1" in main
    assert "Item 2" not in main and "Item 3" not in main
    assert "## Older entries" in main

    rollovers = list((paths.topics_dir / "Archive").glob("China *.md"))
    assert len(rollovers) == 1
    rolled = rollovers[0].read_text(encoding="utf-8")
    assert "Item 2" in rolled and "Item 3" in rolled
    assert "updated_at" not in rolled  # frozen — byte-stable across re-runs

    # second run leaves the frozen rollover untouched (no mtime churn)
    mtime = rollovers[0].stat().st_mtime
    obsidian.write_topic_archive("china", paths)
    assert rollovers[0].stat().st_mtime == mtime


def test_daily_link_diet_keeps_chat_links_on_clipped_only(fresh_db, make_item):
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    db.upsert_items([
        make_item(source="rss", source_id="r1", title="Auto item"),
        make_item(source="clipped", source_id="k1", title="Clipped item"),
    ])
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE items SET topic='ai_capex', triage_decision='keep',
               summary='S', why_it_matters='W', confidence='high', triage_score=0.5"""
        )

    text, item_ids = obsidian.render_daily_note(today)
    assert len(item_ids) == 2
    assert f"_Front page: [[{today} Brief]]_" in text
    # exactly one seeded chat link — the clipped item; the auto item gets `#id`
    assert text.count("https://claude.ai/new?q=") == 1
    with db.get_conn() as conn:
        rss_id = conn.execute(
            "SELECT id FROM items WHERE source_id='r1'"
        ).fetchone()["id"]
    assert f"`#{rss_id}`" in text


def test_weekly_split_moves_items_to_companion_note(fresh_db, make_item):
    from datetime import date, datetime, timezone
    db.upsert_items([make_item(source_id="w1", title="Weekly item one")])
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE items SET topic='ai_semis', triage_decision='keep',
               summary='S', why_it_matters='W', confidence='high', triage_score=0.9"""
        )
    today = datetime.now(timezone.utc).date()
    monday = today.fromordinal(today.toordinal() - today.weekday())
    sunday = date.fromordinal(monday.toordinal() + 6)
    rows = db.items_for_week(monday.isoformat(), sunday.isoformat())
    assert len(rows) == 1
    week_iso = monday.strftime("%G-W%V")
    synthesis = {
        "themes": [{"title": "T1", "description": "D1"}],
        "must_reads": [{"item_id": rows[0]["id"], "reason": "Primary source."}],
    }

    main = obsidian.render_weekly_note(week_iso, monday, sunday, synthesis, rows)
    items = obsidian.render_weekly_items_note(week_iso, monday, sunday, rows)

    # main: synthesis + pointer, no item replay; must-read keeps the chat link
    assert "## 🎯 Themes of the Week" in main
    assert f"[[{obsidian.weekly_items_name(week_iso)}]]" in main
    assert "Weekly item one" not in main.split("## 📌 Must-Reads")[0]
    assert "## 📑 All Items This Week" in main
    assert main.count("https://claude.ai/new?q=") == 1
    # companion: full replay with plain id refs, linking back to synthesis
    assert "Weekly item one" in items
    assert f"`#{rows[0]['id']}`" in items
    assert f"[[{week_iso}]]" in items
    assert "https://claude.ai/new?q=" not in items


def test_topic_archive_cap_disabled_keeps_single_file(fresh_db, make_item, monkeypatch):
    monkeypatch.setattr(db.settings, "obsidian_topic_archive_cap", 0)
    db.upsert_items([make_item(source_id=f"d{i}") for i in range(3)])
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE items SET topic='china', triage_decision='keep',
               summary='S', confidence='high', triage_score=0.5"""
        )
    text, ids = obsidian.render_topic_archive("china")
    assert len(ids) == 3
    assert "## Older entries" not in text
