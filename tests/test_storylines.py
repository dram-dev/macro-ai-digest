"""Storyline threading — persistence, update application, rendering (Wave 2)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from digest import db, obsidian, storylines


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _seed_today_items(make_item, n: int = 2) -> set[int]:
    db.upsert_items([
        make_item(source_id=f"s{i}", title=f"Story item {i}") for i in range(n)
    ])
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE items SET topic='ai_capex', triage_decision='keep',
               summary='S', why_it_matters='W', confidence='high', triage_score=0.8"""
        )
        return {r["id"] for r in conn.execute("SELECT id FROM items").fetchall()}


def test_apply_updates_creates_moves_resolves(fresh_db, make_item):
    today = _today()
    ids = _seed_today_items(make_item)
    id_list = sorted(ids)

    counts = storylines.apply_updates({
        "new": [{
            "slug": "Alphabet Raise!!", "name": "Alphabet utility-style recap",
            "state": "Alphabet raised $84.75B.", "delta": "Raise announced.",
            # 999999 isn't one of today's items — must be filtered out
            "item_ids": [id_list[0], 999999],
        }],
    }, today, ids)
    assert counts == {"moved": 0, "new": 1, "resolved": 0}

    story = db.get_storyline("alphabet-raise")
    assert story is not None
    assert story["status"] == "active"
    assert story["last_moved"] == today
    deltas = db.get_storyline_deltas(story["id"])
    assert len(deltas) == 1
    assert storylines.parse_delta_item_ids(deltas[0]["item_ids"]) == [id_list[0]]

    # next day: move it, then resolve it
    counts = storylines.apply_updates({
        "moved": [{"slug": "alphabet-raise", "delta": "Convert priced.",
                   "state": "Convert priced at utility yields.", "item_ids": id_list}],
        "resolved": [{"slug": "alphabet-raise", "resolution": "Story complete."}],
    }, "2099-01-01", ids)
    assert counts["moved"] == 1 and counts["resolved"] == 1

    story = db.get_storyline("alphabet-raise")
    assert story["status"] == "resolved"
    assert story["state"] == "Convert priced at utility yields."
    assert story["resolution"] == "Story complete."
    assert len(db.get_storyline_deltas(story["id"])) == 2


def test_apply_updates_tolerates_model_sloppiness(fresh_db, make_item):
    today = _today()
    ids = _seed_today_items(make_item)

    # unknown slug move/resolve, empty delta, duplicate "new" slug, >cap new
    db.create_storyline("existing", "Existing story", "State.", today)
    payload = {
        "moved": [
            {"slug": "no-such-slug", "delta": "x", "state": "y"},
            {"slug": "existing", "delta": "", "state": "ignored"},   # empty delta
        ],
        "new": (
            [{"slug": "existing", "name": "Dup", "state": "S2", "delta": "Moved via new.",
              "item_ids": []}]
            + [{"slug": f"extra-{i}", "name": f"E{i}", "state": "s", "delta": "d"}
               for i in range(5)]
        ),
        "resolved": [{"slug": "ghost", "resolution": "n/a"}],
    }
    counts = storylines.apply_updates(payload, today, ids)
    # duplicate "new" became a move of the existing line
    assert counts["moved"] == 1
    assert db.get_storyline("existing")["state"] == "S2"
    # cap respected: the dup consumed one slot, so at most 2 brand-new created
    assert counts["new"] <= storylines.MAX_NEW_PER_DAY - 1
    assert counts["resolved"] == 0


def test_dormancy_and_reactivation(fresh_db):
    db.create_storyline("old-news", "Old news", "State.", "2020-01-01")
    assert db.mark_stale_storylines_dormant(days=14) == 1
    assert db.get_storyline("old-news")["status"] == "dormant"
    # movement reactivates
    db.move_storyline("old-news", "Revived.", _today())
    assert db.get_storyline("old-news")["status"] == "active"
    assert db.mark_stale_storylines_dormant(days=14) == 0


def test_run_storylines_end_to_end_with_mocked_claude(fresh_db, make_item, monkeypatch):
    today = _today()
    ids = _seed_today_items(make_item, n=4)
    reply = json.dumps({
        "moved": [], "resolved": [],
        "new": [{"slug": "chip-curbs", "name": "China chip-curb expansion",
                 "state": "US closing offshore loophole.", "delta": "Loophole closed.",
                 "item_ids": sorted(ids)[:2]}],
    })
    monkeypatch.setattr(storylines, "call_claude", lambda *a, **k: reply)
    counts = storylines.run_storylines(today)
    assert counts["new"] == 1
    assert db.get_storyline("chip-curbs") is not None
    # idempotent re-run: same-day delta is replaced, not duplicated
    storylines.run_storylines(today)
    story = db.get_storyline("chip-curbs")
    assert len(db.get_storyline_deltas(story["id"])) == 1


def test_render_storyline_note_and_index(fresh_db):
    today = _today()
    sid = db.create_storyline("fed-pause", "Fed pause repricing", "Markets reprice.", today)
    db.upsert_storyline_delta(sid, today, "CPI print moved odds.", [12, 34])
    db.create_storyline("done-deal", "Done deal", "Closed.", "2020-01-01")
    db.resolve_storyline("done-deal", "Acquisition closed.")

    story = db.get_storyline("fed-pause")
    text = obsidian.render_storyline_note(story, db.get_storyline_deltas(sid))
    assert "# 📖 Fed pause repricing" in text
    assert "Where this stands" in text
    assert f"### [[{today}]]" in text          # timeline links the daily note
    assert "`#12` · `#34`" in text

    resolved = db.get_storyline("done-deal")
    rtext = obsidian.render_storyline_note(resolved, [])
    assert "Resolved" in rtext and "Acquisition closed." in rtext

    index = obsidian.render_storylines_index(
        db.get_storylines(statuses=("active", "dormant", "resolved"))
    )
    assert "## 🟢 Active" in index and "## ✅ Resolved" in index
    assert "[[Fed pause repricing]]" in index


def test_storyline_note_name_strips_breakers():
    assert obsidian.storyline_note_name('A/B: "C" [D] #E') == "A-B- -C- -D- -E"
    assert obsidian.storyline_note_name("  spaced   out  ") == "spaced out"


def test_brief_shows_todays_movers(fresh_db, make_item):
    today = _today()
    _seed_today_items(make_item)
    sid = db.create_storyline("nvda-vertical", "Nvidia vertical integration", "State.", today)
    db.upsert_storyline_delta(sid, today, "Bought Kumo AI.", [])

    from digest.brief import render_brief_note
    text, _ = render_brief_note(today)
    assert "## 📖 Storylines" in text
    assert "**[[Nvidia vertical integration]]** — Bought Kumo AI." in text


def test_storyline_context_for_weekly(fresh_db):
    db.create_storyline("a-story", "A story", "Its state.", _today())
    ctx = storylines.storyline_context_for_weekly()
    assert ctx == "- A story: Its state."
