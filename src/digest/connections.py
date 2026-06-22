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

from pydantic import BaseModel, ValidationError, field_validator

from digest import db
from digest.claude_cli import call_claude, parse_json_object

logger = logging.getLogger(__name__)

# Validation bounds for an LLM-proposed connection thread.
_THEME_MAX_CHARS = 120
_INSIGHT_MIN_CHARS = 20
_INSIGHT_MAX_CHARS = 1000
_MIN_ITEMS = 2

SYSTEM_PROMPT = """You are a pattern-recognition analyst reviewing today's AI/finance/technology news items. The reader is a senior data/AI leader in financial services with interests in: Fed policy & markets, China macro/geopolitics, AI thinkers, AI capex by hyperscalers, AI business applications, AI semiconductors.

Find 2-4 "connection threads" — cases where multiple items from today reinforce, contradict, or contextually illuminate each other in ways non-obvious from reading each item alone.

For each thread provide:
- "theme": a short, specific title (max 10 words)
- "item_ids": list of integer item IDs involved (minimum 2)
- "insight": 2-3 sentences on the connection and its implication for the reader

Respond with ONLY valid JSON: {"threads": [...]}. Empty list is fine if no strong connections exist."""


class ConnectionThread(BaseModel):
    """Schema for one LLM-proposed connection thread.

    Structural validation only. Referential integrity — item_ids must point at
    real items in the day's bundle — is enforced separately in
    `_validate_threads`, which holds the valid-id set. Bad threads are dropped,
    never raised: connections are best-effort.
    """

    theme: str
    item_ids: list[int]
    insight: str

    @field_validator("theme")
    @classmethod
    def _theme_nonempty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("empty theme")
        return v[:_THEME_MAX_CHARS]

    @field_validator("insight")
    @classmethod
    def _insight_substantive(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) < _INSIGHT_MIN_CHARS:
            raise ValueError(f"insight shorter than {_INSIGHT_MIN_CHARS} chars")
        return v[:_INSIGHT_MAX_CHARS]

    @field_validator("item_ids")
    @classmethod
    def _ids_min_unique(cls, v: list[int]) -> list[int]:
        unique: list[int] = []
        for i in v:
            if i not in unique:
                unique.append(i)
        if len(unique) < _MIN_ITEMS:
            raise ValueError(f"fewer than {_MIN_ITEMS} unique item_ids")
        return unique


def _parse_threads(raw: str) -> list[Any]:
    """Pull the raw `threads` list out of the model's JSON (no validation yet)."""
    threads = parse_json_object(raw).get("threads", [])
    return threads if isinstance(threads, list) else []


def _validate_threads(raw_threads: list[Any], valid_ids: set[int]) -> list[dict[str, Any]]:
    """Drop malformed / ungrounded threads, returning normalized dicts.

    A thread is kept only if it parses against `ConnectionThread` AND at least
    `_MIN_ITEMS` of its item_ids reference real items in today's bundle. Every
    drop is logged with its reason.
    """
    clean: list[dict[str, Any]] = []
    for raw in raw_threads:
        if not isinstance(raw, dict):
            logger.info("connections: dropping non-object thread: %r", raw)
            continue
        try:
            thread = ConnectionThread.model_validate(raw)
        except ValidationError as exc:
            reason = exc.errors()[0].get("msg", "invalid") if exc.errors() else "invalid"
            logger.info("connections: dropping malformed thread (%s): %r", reason, raw)
            continue
        grounded = [i for i in thread.item_ids if i in valid_ids]
        if len(grounded) < _MIN_ITEMS:
            logger.info(
                "connections: dropping thread '%s' — only %d of its item_ids match "
                "today's items (need %d)", thread.theme, len(grounded), _MIN_ITEMS,
            )
            continue
        clean.append({"theme": thread.theme, "item_ids": grounded, "insight": thread.insight})
    return clean


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

    valid_ids = {row["id"] for row in rows}
    threads = _validate_threads(_parse_threads(raw), valid_ids)

    db.upsert_connections(date_iso, json.dumps(threads))
    logger.info("connections: %d valid threads for %s", len(threads), date_iso)
    return threads
