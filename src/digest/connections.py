"""Cross-item connection detection — finds threads linking today's summarized items.

After summarization, takes all items for the day and asks Claude to identify
2-4 "connection threads" where multiple items reinforce, contradict, or
contextually illuminate each other. Results are stored in `daily_connections`
and rendered in the daily note above the topic sections.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from digest import db
from digest.claude_cli import call_claude, parse_json_object

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a pattern-recognition analyst reviewing today's AI/finance/technology news items. The reader is a senior data/AI leader in financial services with interests in: Fed policy & markets, China macro/geopolitics, AI thinkers, AI capex by hyperscalers, AI business applications, AI semiconductors.

Find 2-4 "connection threads" — cases where multiple items from today reinforce, contradict, or contextually illuminate each other in ways non-obvious from reading each item alone.

For each thread provide:
- "theme": a short, specific title (max 10 words)
- "item_ids": list of integer item IDs involved (minimum 2)
- "insight": 2-3 sentences on the connection and its implication for the reader

Respond with ONLY valid JSON: {"threads": [...]}. Empty list is fine if no strong connections exist."""


def _parse_threads(raw: str) -> list[dict[str, Any]]:
    threads = parse_json_object(raw).get("threads", [])
    return threads if isinstance(threads, list) else []


def run_connections(date_iso: str | None = None) -> list[dict[str, Any]]:
    """Detect connection threads across today's summarized items.

    Stores results in the daily_connections table and returns the threads list.
    Returns an empty list (without raising) if there are too few items or
    if the Claude call fails — connections are best-effort, not blocking.
    """
    if date_iso is None:
        date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    bundle = db.items_for_publish(date_iso)
    rows = bundle["summarized"]
    if len(rows) < 4:
        logger.info("connections: %d items on %s — too few, skipping", len(rows), date_iso)
        return []

    item_lines = []
    for row in rows:
        item_lines.append(
            f"ID {row['id']} [{row['topic'] or 'other'}] {row['title']}\n"
            f"  Summary: {(row['summary'] or '')[:300]}\n"
            f"  Why it matters: {(row['why_it_matters'] or '')[:150]}"
        )

    prompt = (
        f"Date: {date_iso}\n"
        f"Items ({len(rows)} total, sorted by topic then triage score):\n\n"
        + "\n\n".join(item_lines)
    )

    try:
        raw = call_claude(SYSTEM_PROMPT, prompt, timeout=120)
    except Exception as exc:
        logger.error("connections: Claude call failed: %s", exc)
        return []

    threads = _parse_threads(raw)
    if not isinstance(threads, list):
        threads = []

    db.upsert_connections(date_iso, json.dumps(threads))
    logger.info("connections: %d threads found for %s", len(threads), date_iso)
    return threads
