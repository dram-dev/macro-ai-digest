"""Prediction scorecard — falsifiable calls, tracked and judged (Wave 3).

The essays, debates, and weekly contrarian signals make concrete calls
("watch the terminal yield on the convert", "underweight pure-play AI
infrastructure") that were never revisited. This module closes the loop:

1. Extraction — when an essay/debate/weekly is generated, one Claude call
   pulls 0-5 falsifiable predictions (claim, observable, direction,
   horizon) into the `predictions` table. Idempotent per document.
2. Resolution — daily, predictions past their horizon are judged in one
   Claude call against the digest's own recent items as evidence. Verdicts:
   correct / incorrect / unclear, with a one-line rationale and evidence
   item IDs. "unclear" stays open for a grace window (later items may
   settle it), then closes as unclear.

Surfaces: Signal/Scorecard.md (hit rate by source, open calls, resolved
log), a weekly right/wrong retro section, and resolutions in the Brief.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from digest import db
from digest.claude_cli import call_claude, parse_json_object

logger = logging.getLogger(__name__)

MAX_PER_DOCUMENT = 5
MIN_HORIZON_DAYS = 7
MAX_HORIZON_DAYS = 180
DEFAULT_HORIZON_DAYS = 30
UNCLEAR_GRACE_DAYS = 7      # unclear verdicts stay open this long past due
EVIDENCE_WINDOW_DAYS = 14
EVIDENCE_MAX_ITEMS = 60
VALID_VERDICTS = {"correct", "incorrect", "unclear"}

EXTRACT_SYSTEM_PROMPT = """You extract falsifiable predictions from analyst writing for later scoring. The text comes from a macro/AI digest's essay, thesis debate, or weekly contrarian note.

A good prediction is one a fair judge could score as right or wrong at a known future date. Skip vibes, descriptions of the present, and unfalsifiable framing.

For each prediction (0-5 per document; fewer is fine, zero is fine):
- "claim": the prediction restated in ≤40 words, SELF-CONTAINED — name the asset, series, company, or actor and the expected outcome. A reader must understand it without the source text.
- "observable": the concrete evidence that would prove or disprove it at the horizon (a print, a price, a filing, an announcement).
- "direction": "up", "down", or "event" (something happens / doesn't).
- "horizon_days": integer 7-180. Market-pricing calls ~30; structural/regulatory shifts ~90-180. If the text names a date or event, use it.

Respond with ONLY valid JSON: {"predictions": [{"claim": "...", "observable": "...", "direction": "...", "horizon_days": 30}]}"""

JUDGE_SYSTEM_PROMPT = """You judge whether past predictions from a macro/AI digest came true, using ONLY the evidence items provided (the digest's recent intake). You have no other knowledge of what happened.

For each prediction return:
- "id": the prediction id
- "verdict": "correct" | "incorrect" | "unclear"
- "rationale": one sentence; cite evidence item IDs in square brackets like [46972] when used
- "evidence_ids": list of the item IDs relied on (empty if none)

Be strict. "correct" or "incorrect" require evidence in the provided items that a skeptical reviewer would accept. If the evidence is absent, mixed, or only tangential, the verdict is "unclear" — that is a normal and common answer.

Respond with ONLY valid JSON: {"verdicts": [{"id": 1, "verdict": "unclear", "rationale": "...", "evidence_ids": []}]}"""


def _clamp_horizon(raw: Any) -> int:
    try:
        h = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_HORIZON_DAYS
    return max(MIN_HORIZON_DAYS, min(MAX_HORIZON_DAYS, h))


def normalize_predictions(
    payload: dict[str, Any], source: str, source_ref: str, made_on: str
) -> list[dict]:
    """Validate extractor output into insertable rows. Sloppy entries dropped."""
    out: list[dict] = []
    made = date.fromisoformat(made_on)
    for entry in (payload.get("predictions") or [])[:MAX_PER_DOCUMENT]:
        if not isinstance(entry, dict):
            continue
        claim = str(entry.get("claim") or "").strip()
        observable = str(entry.get("observable") or "").strip()
        if not claim or not observable:
            continue
        direction = str(entry.get("direction") or "").strip().lower()
        if direction not in {"up", "down", "event"}:
            direction = "event"
        horizon = _clamp_horizon(entry.get("horizon_days"))
        out.append({
            "source": source,
            "source_ref": source_ref,
            "made_on": made_on,
            "due_on": (made + timedelta(days=horizon)).isoformat(),
            "claim": claim[:300],
            "observable": observable[:300],
            "direction": direction,
        })
    return out


def extract_predictions(source: str, source_ref: str, text: str, made_on: str) -> int:
    """Extract and store predictions from one document. Returns rows inserted.

    Best-effort: returns 0 on any failure — generation flows never block on this.
    """
    if not (text or "").strip():
        return 0
    prompt = f"Source: {source} ({source_ref}), written {made_on}\n\nText:\n\n{text[:12000]}"
    try:
        raw = call_claude(EXTRACT_SYSTEM_PROMPT, prompt, timeout=120)
        preds = normalize_predictions(parse_json_object(raw), source, source_ref, made_on)
    except Exception as exc:
        logger.error("predictions: extraction failed for %s %s: %s", source, source_ref, exc)
        return 0
    inserted = db.insert_predictions(preds)
    logger.info(
        "predictions: %s %s — %d extracted, %d new", source, source_ref, len(preds), inserted
    )
    return inserted


def _evidence_block(date_iso: str) -> str:
    start = (date.fromisoformat(date_iso) - timedelta(days=EVIDENCE_WINDOW_DAYS)).isoformat()
    rows = db.items_for_week(start, date_iso)[:EVIDENCE_MAX_ITEMS]
    lines = []
    for row in rows:
        lines.append(
            f"[{row['id']}] {(row['ingested_at'] or '')[:10]} {row['title']}\n"
            f"  {(row['summary'] or '')[:220]}"
        )
    return "\n".join(lines) or "(no recent items)"


def _clean_evidence_ids(raw: Any) -> list[int]:
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for v in raw:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out[:10]


def apply_verdicts(
    verdicts: list[dict], due_rows: list, date_iso: str
) -> dict[str, int]:
    """Apply judge verdicts to the due predictions. Returns counts by outcome.

    Unknown IDs are ignored. "unclear" only closes a prediction once it is
    UNCLEAR_GRACE_DAYS past due; otherwise it stays open for a later retry.
    """
    by_id = {row["id"]: row for row in due_rows}
    today = date.fromisoformat(date_iso)
    counts = {"correct": 0, "incorrect": 0, "unclear": 0, "deferred": 0}

    for entry in verdicts:
        if not isinstance(entry, dict):
            continue
        try:
            pid = int(entry.get("id"))
        except (TypeError, ValueError):
            continue
        row = by_id.get(pid)
        verdict = str(entry.get("verdict") or "").strip().lower()
        if row is None or verdict not in VALID_VERDICTS:
            continue
        rationale = str(entry.get("rationale") or "").strip()[:400]
        evidence = _clean_evidence_ids(entry.get("evidence_ids"))

        if verdict == "unclear":
            grace_end = date.fromisoformat(row["due_on"]) + timedelta(days=UNCLEAR_GRACE_DAYS)
            if today < grace_end:
                counts["deferred"] += 1   # leave open; retry on a later day
                continue
            rationale = rationale or "No decisive evidence within the grace window."

        db.resolve_prediction(pid, verdict, rationale, evidence, date_iso)
        counts[verdict] += 1
    return counts


def resolve_due_predictions(date_iso: str | None = None) -> dict[str, int]:
    """Judge predictions past their horizon. One Claude call; zero if none due."""
    if date_iso is None:
        date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    zero = {"due": 0, "correct": 0, "incorrect": 0, "unclear": 0, "deferred": 0}
    due_rows = db.open_predictions(due_by=date_iso)
    if not due_rows:
        return zero

    pred_lines = [
        f"id={row['id']} made {row['made_on']} due {row['due_on']} "
        f"[{row['source']} {row['source_ref']}]\n"
        f"  Claim: {row['claim']}\n  Observable: {row['observable']}"
        for row in due_rows
    ]
    prompt = (
        f"Today: {date_iso}\n\n"
        f"Predictions to judge ({len(due_rows)}):\n\n" + "\n\n".join(pred_lines)
        + f"\n\nEvidence — digest items from the last {EVIDENCE_WINDOW_DAYS} days:\n\n"
        + _evidence_block(date_iso)
    )

    try:
        raw = call_claude(JUDGE_SYSTEM_PROMPT, prompt, timeout=180)
    except Exception as exc:
        logger.error("predictions: judge call failed: %s", exc)
        return zero

    verdicts = parse_json_object(raw).get("verdicts") or []
    counts = apply_verdicts(verdicts, due_rows, date_iso)
    counts["due"] = len(due_rows)
    logger.info(
        "predictions: %s due=%d correct=%d incorrect=%d unclear=%d deferred=%d",
        date_iso, counts["due"], counts["correct"], counts["incorrect"],
        counts["unclear"], counts["deferred"],
    )
    return counts


def hit_rate(stats: dict[str, dict[str, int]] | None = None) -> tuple[int, int]:
    """(correct, resolved-with-verdict) across all sources; unclear excluded."""
    stats = stats if stats is not None else db.prediction_stats()
    correct = sum(s.get("correct", 0) for s in stats.values())
    wrong = sum(s.get("incorrect", 0) for s in stats.values())
    return correct, correct + wrong
