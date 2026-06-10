"""Prediction scorecard — extraction normalization, judging, rendering (Wave 3)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

from digest import db, obsidian, predictions


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _insert(claim: str = "NVDA reclaims $220 within a month.", **over) -> int:
    row = {
        "source": "essay", "source_ref": "2026-06-06", "made_on": "2026-06-06",
        "due_on": "2026-07-06", "claim": claim,
        "observable": "NVDA closing price vs $220.", "direction": "up",
    } | over
    assert db.insert_predictions([row]) == 1
    with db.get_conn() as conn:
        return conn.execute("SELECT MAX(id) AS i FROM predictions").fetchone()["i"]


def test_normalize_predictions_validates_and_caps(fresh_db):
    payload = {"predictions": [
        {"claim": "AMD rerates lower as China TAM closes.",
         "observable": "AMD price vs 50-DMA.", "direction": "DOWN", "horizon_days": 30},
        {"claim": "", "observable": "x"},                      # dropped: empty claim
        {"claim": "y", "observable": ""},                      # dropped: empty observable
        {"claim": "Bad horizon.", "observable": "z", "horizon_days": 9999},
        "not-a-dict",
    ] + [{"claim": f"c{i}", "observable": "o"} for i in range(10)]}
    rows = predictions.normalize_predictions(payload, "essay", "2026-06-06", "2026-06-06")
    # cap applies to the raw first 5 entries; of those, only 2 are valid
    assert [r["claim"] for r in rows] == [
        "AMD rerates lower as China TAM closes.", "Bad horizon.",
    ]
    assert rows[0]["direction"] == "down"
    assert rows[0]["due_on"] == "2026-07-06"
    # horizon clamped to max; missing direction defaults to event
    bad = rows[1]
    assert bad["direction"] == "event"
    assert bad["due_on"] == (
        date(2026, 6, 6) + timedelta(days=predictions.MAX_HORIZON_DAYS)
    ).isoformat()


def test_insert_predictions_idempotent(fresh_db):
    _insert()
    again = predictions.normalize_predictions(
        {"predictions": [{"claim": "NVDA reclaims $220 within a month.",
                          "observable": "different obs"}]},
        "essay", "2026-06-06", "2026-06-06",
    )
    assert db.insert_predictions(again) == 0     # same (source, ref, claim) → ignored
    assert len(db.all_predictions()) == 1


def test_apply_verdicts_grace_window(fresh_db):
    today = _today()
    # due long ago → unclear closes; due yesterday → unclear defers (stays open)
    old = _insert(claim="Old call.", due_on="2020-01-01")
    fresh = _insert(
        claim="Fresh call.", source_ref="2026-06-07",
        due_on=(date.fromisoformat(today) - timedelta(days=1)).isoformat(),
    )
    due = db.open_predictions(due_by=today)
    counts = predictions.apply_verdicts(
        [
            {"id": old, "verdict": "unclear", "rationale": "", "evidence_ids": []},
            {"id": fresh, "verdict": "unclear", "rationale": "", "evidence_ids": []},
            {"id": 424242, "verdict": "correct"},          # unknown id ignored
            {"id": fresh, "verdict": "banana"},            # invalid verdict ignored
        ],
        due, today,
    )
    assert counts == {"correct": 0, "incorrect": 0, "unclear": 1, "deferred": 1}
    by_id = {r["id"]: r for r in db.all_predictions()}
    assert by_id[old]["status"] == "unclear"
    assert by_id[old]["rationale"]                          # default grace rationale
    assert by_id[fresh]["status"] == "open"


def test_resolve_due_predictions_with_mocked_judge(fresh_db, make_item, monkeypatch):
    today = _today()
    db.upsert_items([make_item(source_id="e1", title="Evidence item")])
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE items SET topic='ai_semis', triage_decision='keep',
               summary='NVDA closed at 230.', confidence='high', triage_score=0.9"""
        )
        evidence_id = conn.execute("SELECT id FROM items").fetchone()["id"]

    pid = _insert(due_on="2020-01-01")
    reply = json.dumps({"verdicts": [
        {"id": pid, "verdict": "correct",
         "rationale": f"Reclaimed per [{evidence_id}].", "evidence_ids": [evidence_id]},
    ]})
    monkeypatch.setattr(predictions, "call_claude", lambda *a, **k: reply)
    counts = predictions.resolve_due_predictions(today)
    assert counts["due"] == 1 and counts["correct"] == 1

    row = db.all_predictions()[0]
    assert row["status"] == "correct"
    assert row["resolved_on"] == today
    assert json.loads(row["evidence_ids"]) == [evidence_id]
    # nothing due → no Claude call needed, zero counts
    assert predictions.resolve_due_predictions(today)["due"] == 0


def test_hit_rate_excludes_unclear(fresh_db):
    a = _insert(claim="A.")
    b = _insert(claim="B.", source_ref="x")
    c = _insert(claim="C.", source_ref="y", source="debate")
    db.resolve_prediction(a, "correct", "r", [], _today())
    db.resolve_prediction(b, "unclear", "r", [], _today())
    db.resolve_prediction(c, "incorrect", "r", [], _today())
    assert predictions.hit_rate() == (1, 2)


def test_render_scorecard_note(fresh_db):
    open_id = _insert(claim="Open call.")
    done = _insert(claim="Done call.", source_ref="z", source="weekly")
    db.resolve_prediction(done, "incorrect", "Contradicted by prints.", [12], _today())

    text = obsidian.render_scorecard_note()
    assert "# 🧾 Prediction Scorecard" in text
    assert "## ⏳ Open Calls (1)" in text
    assert "`due 2026-07-06` Open call. *(essay 2026-06-06)*" in text
    assert "## 📜 Resolved (1)" in text
    assert "❌ Done call. *(weekly z)*" in text
    assert "Contradicted by prints." in text
    assert "**0%** hit rate (0/1 resolved)" in text
    assert open_id  # silence unused warning


def test_brief_scoreboard_includes_resolved_calls(fresh_db, make_item):
    db.upsert_items([make_item(source_id="b1", title="T")])
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE items SET topic='china', triage_decision='keep', summary='S',
               confidence='high', triage_score=0.5"""
        )
    pid = _insert(claim="Resolved call.")
    db.resolve_prediction(pid, "correct", "r", [], _today())

    from digest.brief import render_brief_note
    text, _ = render_brief_note(_today())
    assert "## 🔁 Scoreboard — resolved since yesterday" in text
    assert "✅ **call correct** — Resolved call. *(essay 2026-06-06)*" in text


def test_weekly_retro_section(fresh_db, make_item):
    db.upsert_items([make_item(source_id="w1", title="Weekly item")])
    with db.get_conn() as conn:
        conn.execute(
            """UPDATE items SET topic='ai_capex', triage_decision='keep', summary='S',
               why_it_matters='W', confidence='high', triage_score=0.9"""
        )
    today = datetime.now(timezone.utc).date()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    pid = _insert(claim="Weekly judged call.")
    db.resolve_prediction(pid, "correct", "Held up.", [], today.isoformat())

    rows = db.items_for_week(monday.isoformat(), sunday.isoformat())
    week_iso = monday.strftime("%G-W%V")
    text = obsidian.render_weekly_note(week_iso, monday, sunday, {"themes": []}, rows)
    assert "## 🧾 Scorecard" in text
    assert "✅ Weekly judged call." in text
    assert "[[Scorecard]]" in text
