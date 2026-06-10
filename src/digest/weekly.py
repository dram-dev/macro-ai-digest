"""Weekly synthesis — produces a briefing across the full week's summarized items.

Called by `digest weekly` (CLI) → `obsidian.publish_weekly()`. Separate from
obsidian.py to keep the Claude call logic in one layer and rendering in another.
"""
from __future__ import annotations

import logging
from typing import Any

from digest.claude_cli import call_claude, parse_json_object

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior research analyst writing a weekly executive briefing for a data/AI leader in financial services. The reader's key interests: Fed policy & markets, China macro/geopolitics, AI thinkers, AI capex by hyperscalers, AI business applications, AI semiconductors.

You receive the week's top summarized items, pre-sorted by triage score. Your job is to synthesize across items — not re-summarize each one.

Return ONLY valid JSON with these exact keys:
{
  "themes": [
    {"title": "short theme title", "description": "2-3 sentences on this week's dominant theme"}
  ],
  "must_reads": [
    {"item_id": 1234, "reason": "one sentence on why this is the most important item in its area"}
  ],
  "contrarian_signal": "1-2 sentences on an underappreciated or counterintuitive signal this week",
  "macro_ai_intersection": "1-2 sentences on the most important macro/AI connection this week"
}

themes: 3-5 items. must_reads: exactly 5 items (pick the 5 highest-signal items across different topic areas)."""


def synthesize_week(
    rows: list,
    week_label: str,
    regime_framing: str = "",
    storyline_context: str = "",
) -> dict[str, Any]:
    """Call Claude to produce a weekly synthesis over the given item rows.

    Args:
        rows: sqlite3.Row list from db.items_for_week(), pre-sorted by triage_score DESC.
        week_label: human label like "2026-W20 (May 11 – May 17)".
        regime_framing: optional macro regime context injected at prompt top.
        storyline_context: optional "- Name: state" lines for the active
            storylines, so themes extend tracked narratives instead of
            rediscovering them from scratch each week.

    Returns:
        Parsed synthesis dict with keys: themes, must_reads, contrarian_signal,
        macro_ai_intersection. Empty dict on failure.
    """
    if not rows:
        return {}

    # Limit prompt size: top 30 items by triage score
    top = rows[:30]
    item_lines = []
    for row in top:
        item_lines.append(
            f"ID {row['id']} [{row['topic'] or 'other'}] score={float(row['triage_score'] or 0):.2f}\n"
            f"  Title: {row['title']}\n"
            f"  Summary: {(row['summary'] or '')[:300]}\n"
            f"  Why: {(row['why_it_matters'] or '')[:150]}"
        )

    header = f"Week: {week_label}\n"
    if regime_framing:
        header += f"Macro regime context: {regime_framing}\n"
    if storyline_context:
        header += (
            "Active storylines the reader is already tracking (when a theme "
            "continues one of these, say so by name and focus on what changed "
            "this week rather than re-introducing the story):\n"
            f"{storyline_context}\n"
        )
    header += f"Total items with summaries: {len(rows)} (showing top {len(top)} by triage score)\n"

    prompt = header + "\n" + "\n\n".join(item_lines)

    try:
        raw = call_claude(SYSTEM_PROMPT, prompt, timeout=180)
    except Exception as exc:
        logger.error("weekly: Claude call failed: %s", exc)
        return {}

    synthesis = parse_json_object(raw)
    if not synthesis:
        logger.warning("weekly: unparseable synthesis response")
    return synthesis
