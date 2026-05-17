"""Phase 2 — Triage. Local Qwen (Ollama) decides keep/drop and assigns a topic.

The triage step is the cost gate: it filters the firehose down to the
~15-25 items per run that warrant Claude-quality summarization. Local model
means it's fast and free — we can afford to triage every ingested item.

Flow (per item):
    1. Build a compact prompt: title, source, metadata, first ~600 chars of content
    2. Ask Qwen for a JSON verdict: {decision, score, topic, reason}
    3. Persist decision via db.update_triage()

The CLI command `digest triage` (added in cli.py) runs this on all
items where triage_decision IS NULL.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import time
from typing import Any

import requests

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)

OLLAMA_GENERATE_URL = "{host}/api/generate"
REQUEST_TIMEOUT_SEC = 60

# Topic taxonomy. Triage maps every kept item to exactly one of these.
# Mirrors the Obsidian topic archives in 80 Digest/Topics/.
TOPICS = [
    "fed_markets",
    "china",
    "ai_thinkers",
    "ai_capex",
    "ai_business_apps",
    "ai_semis",
    "data_viz",
    "other",   # catch-all; "other" + low score = drop
]

SYSTEM_PROMPT = """You are a strict triage filter for a daily personal digest.
Your job: decide if a piece of content is worth the user's attention given their interests.

The user is a senior data/AI leader in financial services. Their interests:
- Fed policy, US/global markets, macro indicators (high signal)
- China macro + geopolitics (high signal)
- AI thinkers (Karpathy, Mollick, Willison, Weng, Lambert, Dwarkesh) — original analysis
- AI capex by hyperscalers (MSFT, GOOG, AMZN, META, ORCL, NVDA earnings, datacenter buildouts)
- AI business applications — non-obvious, downstream, novel use cases
- AI semis — datacenter GPUs, inference economics, supply chain
- Data visualization techniques and chart inspiration

DROP signals (set decision="drop"):
- Pure consumer tech reviews, gaming, mobile phone news
- Crypto / NFTs / meme tokens
- Generic "AI is going to take all jobs" speculation without analysis
- Off-topic Reddit drama, low-effort posts, tutorials, beginner questions
- Duplicate of something already obvious (e.g. "Fed held rates steady" with no new analysis)
- US politics unless directly market-moving or directly about China policy

KEEP signals (set decision="keep"):
- Original analysis from named thinkers
- New data points (filings, FRED prints, earnings details)
- Non-obvious second-order effects or contrarian takes
- Specific dollar/percentage/technical numbers that change the user's mental model

Respond with ONLY a single JSON object, nothing else. Schema:
{
  "decision": "keep" | "drop",
  "score": 0.0 to 1.0,
  "topic": one of the topics list,
  "reason": "one sentence, max 15 words"
}
"""

USER_TEMPLATE = """Topics available: {topics}

Source: {source}
Title: {title}
Author: {author}
Published: {published}
Topic hint from ingestor: {topic_hint}

Content excerpt:
{content}

JSON verdict:"""


def _build_prompt(item: dict[str, Any]) -> str:
    metadata = json.loads(item.get("metadata_json") or "{}")
    content = (item.get("content") or "").strip()
    if len(content) > 600:
        content = content[:600] + "…"
    if not content:
        content = "(no body content; title-only)"

    return USER_TEMPLATE.format(
        topics=", ".join(TOPICS),
        source=item.get("source", "?"),
        title=item.get("title", "?"),
        author=item.get("author") or "(unknown)",
        published=(item.get("published_at") or "")[:19],
        topic_hint=metadata.get("topic_hint") or metadata.get("group") or "(none)",
        content=content,
    )


def _extract_json(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction. Qwen sometimes wraps output or adds prose."""
    raw = raw.strip()
    # Common case: pure JSON
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Look for fenced code block
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # Last-ditch: first balanced object
    m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _normalize_verdict(verdict: dict[str, Any]) -> dict[str, Any]:
    decision = str(verdict.get("decision", "drop")).lower().strip()
    if decision not in ("keep", "drop"):
        decision = "drop"

    raw_score = verdict.get("score", 0.0)
    try:
        score = float(raw_score)
    except (TypeError, ValueError):
        score = 0.0
    score = max(0.0, min(1.0, score))

    # Low-confidence "keep" verdicts are downgraded based on TRIAGE_MIN_SCORE.
    if decision == "keep" and score < settings.triage_min_score:
        decision = "drop"

    topic = str(verdict.get("topic", "other")).lower().strip()
    if topic not in TOPICS:
        topic = "other"

    return {
        "decision": decision,
        "score": score,
        "topic": topic,
        "reason": str(verdict.get("reason", ""))[:200],
    }


def _ollama_call(prompt: str) -> str:
    """Single non-streaming Ollama generation. Returns raw response text."""
    url = OLLAMA_GENERATE_URL.format(host=settings.ollama_host.rstrip("/"))
    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "system": SYSTEM_PROMPT,
        "stream": False,
        "format": "json",  # nudges Qwen toward valid JSON
        "options": {
            "temperature": 0.1,   # low for consistency
            "num_predict": 256,
            "num_ctx": 4096,
        },
    }
    r = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT_SEC)
    r.raise_for_status()
    return r.json().get("response", "")


_DEDUP_THRESHOLD = 0.85


def _dedup_match(title: str, seen: list[str]) -> str | None:
    """Return the first seen title with SequenceMatcher ratio ≥ threshold, else None."""
    t = title.lower()
    for s in seen:
        if difflib.SequenceMatcher(None, t, s.lower()).ratio() >= _DEDUP_THRESHOLD:
            return s
    return None


def triage_item(item: dict[str, Any]) -> dict[str, Any]:
    """Run triage on one item. Returns the normalized verdict."""
    prompt = _build_prompt(item)
    raw = _ollama_call(prompt)
    verdict = _extract_json(raw) or {}
    if not verdict:
        logger.warning("triage: failed to parse Qwen output for item %s", item.get("id"))
        # Conservative fallback: drop the item but tag for retry
        return {"decision": "drop", "score": 0.0, "topic": "other", "reason": "parse_error"}
    return _normalize_verdict(verdict)


def run_triage(limit: int = 200) -> dict[str, int]:
    """Triage all pending items (up to `limit`). Returns counts by decision."""
    # Auto-keep quantitative items first so they never reach Qwen.
    auto_kept = db.auto_keep_quantitative()
    if auto_kept:
        logger.info("triage: auto-kept %d quantitative items (bypassing Qwen)", auto_kept)

    items = db.items_needing_triage(limit=limit)
    if not items:
        logger.info("triage: nothing pending")
        return {"pending": 0, "kept": auto_kept, "dropped": 0, "errors": 0}

    # Seed seen_titles from DB (kept items in the last 24h) for cross-run dedup.
    # Items kept earlier in this same batch are appended as we go.
    seen_titles = db.recent_kept_titles(hours=24)

    counts = {"pending": len(items), "kept": 0, "dropped": 0, "errors": 0}
    for row in items:
        item_dict = dict(row)
        title = item_dict.get("title") or ""
        try:
            # Dedup check: skip Qwen if this title is too similar to a kept item.
            match = _dedup_match(title, seen_titles)
            if match:
                db.update_triage(
                    item_id=item_dict["id"],
                    decision="drop",
                    score=0.0,
                    topic="other",
                )
                counts["dropped"] += 1
                logger.info(
                    "triage: id=%d drop/dedup — matches: %.60s",
                    item_dict["id"], match,
                )
                continue

            t0 = time.perf_counter()
            verdict = triage_item(item_dict)
            elapsed = time.perf_counter() - t0
            db.update_triage(
                item_id=item_dict["id"],
                decision=verdict["decision"],
                score=verdict["score"],
                topic=verdict["topic"],
            )
            if verdict["decision"] == "keep":
                counts["kept"] += 1
                seen_titles.append(title)  # track for in-batch dedup
            else:
                counts["dropped"] += 1
            logger.info(
                "triage: id=%d %s/%.2f topic=%s (%.1fs) — %s",
                item_dict["id"],
                verdict["decision"],
                verdict["score"],
                verdict["topic"],
                elapsed,
                verdict.get("reason", ""),
            )
        except Exception as exc:  # noqa: BLE001
            counts["errors"] += 1
            logger.exception("triage: id=%s failed: %s", item_dict.get("id"), exc)
    return counts
