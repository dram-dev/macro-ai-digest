"""Storyline threading — persistent narratives tracked across days (Wave 2).

The dailies are amnesiac: a story like an equity raise evolves over a week
(announce → upsize → anchor investor → selloff → thesis) but each note sees
only its own day. This pass gives the digest memory. After summarization,
one Claude call compares today's items against the open storylines and
returns which moved (with a what-changed-today delta + refreshed running
state), which new narratives to open, and which to resolve.

Persistence: `storylines` (slug, name, status active/dormant/resolved,
running state) + `storyline_deltas` (one per storyline per day, with the
item IDs that moved it). Rendering lives in obsidian.py (Storylines/ pages
+ index); the Brief shows today's movers; weekly themes are seeded from
active storyline states.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from digest import db
from digest.claude_cli import call_claude, parse_json_object

logger = logging.getLogger(__name__)

MAX_ACTIVE_IN_PROMPT = 12   # most recently moved active+dormant lines shown to the model
MAX_NEW_PER_DAY = 3
DORMANT_AFTER_DAYS = 14
_MAX_STATE_CHARS = 900
_MAX_DELTA_CHARS = 400


def _clip(text: Any, limit: int) -> str:
    """Trim to a word boundary with an ellipsis instead of a mid-word slice."""
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip() + "…"

SYSTEM_PROMPT = """You are a narrative tracker for a macro/AI digest. The reader is a senior data/AI leader in financial services tracking: Fed policy & markets, China macro/geopolitics, AI thinkers, AI capex by hyperscalers, AI business applications, AI semiconductors.

You maintain STORYLINES: specific, evolving multi-day stories — an equity raise being repriced, an export-control push, a model-release fallout, an M&A arc. A storyline is NOT a permanent topic ("AI capex" is a topic; "Alphabet's $84.75B equity raise and its utility-style repricing" is a storyline).

You receive the currently tracked storylines (slug, status, running state) and today's digest items. Decide:

1. "moved" — existing storylines that today's items genuinely advance. For each: the slug, a "delta" (1-2 sentences: what changed TODAY), an updated "state" (2-4 sentences: the full running story so a reader landing here cold understands it), and the item_ids that moved it. Movement must come from today's items — do not invent developments. A dormant storyline may be moved if today's items genuinely revive it.
2. "new" — at most 3 storylines worth opening: stories likely to develop over days/weeks with concrete future checkpoints (earnings, hearings, closings, pricings). For each: a "slug" (kebab-case, stable), a "name" (max 8 words, specific), "state", "delta", "item_ids". Do not open a storyline that duplicates an existing one — move the existing one instead.
3. "resolved" — storylines that have concluded or become permanently stale. For each: the slug and a one-sentence "resolution".

Be conservative: most days most storylines do not move, and zero new storylines is a normal answer.

Respond with ONLY valid JSON:
{"moved": [{"slug": "...", "delta": "...", "state": "...", "item_ids": [1,2]}],
 "new": [{"slug": "...", "name": "...", "delta": "...", "state": "...", "item_ids": [3]}],
 "resolved": [{"slug": "...", "resolution": "..."}]}"""


def _slugify(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (raw or "").lower()).strip("-")
    return slug[:60]


def _clean_ids(raw: Any, allowed: set[int]) -> list[int]:
    """Keep only integer IDs that are actually among today's items."""
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for v in raw:
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        if iv in allowed and iv not in out:
            out.append(iv)
    return out


def apply_updates(
    payload: dict[str, Any], date_iso: str, today_ids: set[int]
) -> dict[str, int]:
    """Validate and persist one day's storyline updates. Returns counts.

    Tolerant of model sloppiness: unknown slugs are skipped on move/resolve,
    a "new" storyline whose slug already exists is treated as a move, item
    IDs are filtered to today's set, and new storylines are capped.
    """
    counts = {"moved": 0, "new": 0, "resolved": 0}

    moves = list(payload.get("moved") or [])
    for entry in list(payload.get("new") or [])[:MAX_NEW_PER_DAY]:
        slug = _slugify(str(entry.get("slug") or entry.get("name") or ""))
        if not slug:
            continue
        if db.get_storyline(slug):
            moves.append(entry | {"slug": slug})
            continue
        name = _clip(entry.get("name"), 80) or slug.replace("-", " ").title()
        state = _clip(entry.get("state"), _MAX_STATE_CHARS)
        delta = _clip(entry.get("delta"), _MAX_DELTA_CHARS)
        if not state or not delta:
            continue
        sid = db.create_storyline(slug, name, state, date_iso)
        db.upsert_storyline_delta(sid, date_iso, delta, _clean_ids(entry.get("item_ids"), today_ids))
        counts["new"] += 1

    for entry in moves:
        slug = _slugify(str(entry.get("slug") or ""))
        row = db.get_storyline(slug) if slug else None
        if not row or row["status"] == "resolved":
            continue
        delta = _clip(entry.get("delta"), _MAX_DELTA_CHARS)
        if not delta:
            continue
        state = _clip(entry.get("state"), _MAX_STATE_CHARS) or row["state"]
        db.move_storyline(slug, state, date_iso)
        db.upsert_storyline_delta(row["id"], date_iso, delta, _clean_ids(entry.get("item_ids"), today_ids))
        counts["moved"] += 1

    for entry in payload.get("resolved") or []:
        slug = _slugify(str(entry.get("slug") or ""))
        row = db.get_storyline(slug) if slug else None
        if not row or row["status"] == "resolved":
            continue
        resolution = _clip(entry.get("resolution"), _MAX_DELTA_CHARS)
        db.resolve_storyline(slug, resolution or "Concluded.")
        counts["resolved"] += 1

    return counts


def _build_prompt(date_iso: str, stories: list, rows: list) -> str:
    story_lines = []
    for s in stories:
        story_lines.append(
            f"- slug={s['slug']} [{s['status']}] {s['name']}\n  State: {s['state']}"
        )
    item_lines = []
    for row in rows:
        item_lines.append(
            f"ID {row['id']} [{row['topic'] or 'other'}] {row['title']}\n"
            f"  Summary: {(row['summary'] or '')[:300]}"
        )
    return (
        f"Date: {date_iso}\n\n"
        f"Tracked storylines ({len(stories)}):\n"
        + ("\n".join(story_lines) if story_lines else "(none yet)")
        + f"\n\nToday's items ({len(rows)}):\n\n"
        + "\n\n".join(item_lines)
    )


def run_storylines(date_iso: str | None = None) -> dict[str, int]:
    """Update storylines from today's summarized items. Best-effort, non-blocking.

    Returns counts {moved, new, resolved, dormant}; zeros if skipped or failed.
    """
    if date_iso is None:
        date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    zero = {"moved": 0, "new": 0, "resolved": 0, "dormant": 0}
    rows = db.items_for_publish(date_iso)["summarized"]
    if len(rows) < 4:
        logger.info("storylines: %d items on %s — too few, skipping", len(rows), date_iso)
        return zero

    stories = db.get_storylines(statuses=("active", "dormant"), limit=MAX_ACTIVE_IN_PROMPT)
    prompt = _build_prompt(date_iso, stories, rows)

    try:
        raw = call_claude(SYSTEM_PROMPT, prompt, timeout=180)
    except Exception as exc:
        logger.error("storylines: Claude call failed: %s", exc)
        return zero

    payload = parse_json_object(raw)
    if not payload:
        logger.warning("storylines: unparseable response")
        return zero

    counts = apply_updates(payload, date_iso, {r["id"] for r in rows})
    counts["dormant"] = db.mark_stale_storylines_dormant(DORMANT_AFTER_DAYS)
    logger.info(
        "storylines: %s moved=%d new=%d resolved=%d dormant=%d",
        date_iso, counts["moved"], counts["new"], counts["resolved"], counts["dormant"],
    )
    return counts


def storyline_context_for_weekly(limit: int = 10) -> str:
    """'- Name: state' lines for active storylines (weekly theme seeding)."""
    stories = db.get_storylines(statuses=("active",), limit=limit)
    return "\n".join(f"- {s['name']}: {s['state']}" for s in stories)


def parse_delta_item_ids(raw: str | None) -> list[int]:
    """Item-ID list stored on a delta row; [] on missing/invalid."""
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [int(v) for v in val] if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError, ValueError):
        return []
