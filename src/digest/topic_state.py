"""Topic state-of-play — a rolling synthesis header per topic (Wave 4).

Topic pages were pure archives: 200 reverse-chronological items with no way
to answer "where does this topic stand?" without reading them. This pass
maintains a short per-topic brief — standing thesis, what changed this week,
what to watch — refreshed weekly by one Claude call across all topics and
rendered as a callout above each archive (obsidian._render_archive_doc).

State lives in the `topic_state` table; the previous state is fed back into
the prompt so the thesis evolves rather than being rediscovered weekly.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from digest import db
from digest.claude_cli import call_claude, parse_json_object

logger = logging.getLogger(__name__)

ITEMS_PER_TOPIC = 10
WINDOW_DAYS = 7
_MAX_FIELD_CHARS = 800

SYSTEM_PROMPT = """You maintain per-topic "state of play" briefs for a macro/AI digest. The reader is a senior data/AI leader in financial services. Topics: fed_markets, china, ai_thinkers, ai_capex, ai_business_apps, ai_semis, data_viz, other.

For each topic you receive the current brief (possibly none) and this week's top items. Produce an updated brief per topic:

- "state": 2-4 sentences. The standing thesis — where this topic is right now, readable cold by someone who skipped a month. Carry forward what still holds from the previous state; revise what the new evidence changed.
- "changed": 1-2 sentences. What actually moved THIS WEEK. If nothing meaningful moved, say so plainly.
- "watch": 1 sentence. The most important open question or upcoming checkpoint.

Write tight, specific prose — names, numbers, dates. No "in a tightening macro regime" filler. Only include topics you were given items or a previous state for.

Respond with ONLY valid JSON:
{"states": {"<topic>": {"state": "...", "changed": "...", "watch": "..."}}}"""


def _clip(text: Any, limit: int = _MAX_FIELD_CHARS) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip() + "…"


def _build_prompt(date_iso: str, week_iso: str, topics: list[str]) -> str:
    start = (date.fromisoformat(date_iso) - timedelta(days=WINDOW_DAYS)).isoformat()
    rows = db.items_for_week(start, date_iso)
    by_topic: dict[str, list] = {}
    for row in rows:
        slug = row["topic"] or "other"
        if len(by_topic.setdefault(slug, [])) < ITEMS_PER_TOPIC:
            by_topic[slug].append(row)

    blocks: list[str] = [f"Week: {week_iso} (items from {start} to {date_iso})\n"]
    for slug in topics:
        prev = db.get_topic_state(slug)
        items = by_topic.get(slug, [])
        if not prev and not items:
            continue
        blocks.append(f"## Topic: {slug}")
        blocks.append(
            f"Previous state ({prev['week']}): {prev['state']}" if prev
            else "Previous state: (none — first brief)"
        )
        if items:
            for row in items:
                blocks.append(
                    f"- [{(row['ingested_at'] or '')[:10]}] {row['title']}\n"
                    f"  {(row['summary'] or '')[:220]}"
                )
        else:
            blocks.append("(no items this week)")
        blocks.append("")
    return "\n".join(blocks)


def run_topic_states(date_iso: str | None = None) -> int:
    """Refresh every topic's state-of-play brief. Returns topics updated.

    Best-effort: returns 0 on failure — callers never block on this.
    """
    if date_iso is None:
        date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_iso = date.fromisoformat(date_iso).strftime("%G-W%V")

    topics = db.topics_with_summaries()
    if not topics:
        return 0

    prompt = _build_prompt(date_iso, week_iso, topics)
    try:
        raw = call_claude(SYSTEM_PROMPT, prompt, timeout=180)
    except Exception as exc:
        logger.error("topic_state: Claude call failed: %s", exc)
        return 0

    states = parse_json_object(raw).get("states") or {}
    updated = 0
    for slug, entry in states.items():
        if slug not in topics or not isinstance(entry, dict):
            continue
        state = _clip(entry.get("state"))
        if not state:
            continue
        db.upsert_topic_state(
            slug, state, _clip(entry.get("changed")), _clip(entry.get("watch")), week_iso
        )
        updated += 1
    logger.info("topic_state: %s — %d topics updated", week_iso, updated)
    return updated
