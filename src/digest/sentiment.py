"""Financial sentiment classification via MLX (Feature 1).

Uses a structured JSON prompt to classify each kept+summarized item as
bullish / bearish / neutral with a confidence score. Results stored as
sentiment_label and sentiment_score on the items table.

Run via: digest sentiment
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import requests

from digest import db
from digest.config import settings
from digest_core.summarize.backends import mlx_serialize

logger = logging.getLogger(__name__)

_SYSTEM = """\
You are a financial market analyst. Classify the market sentiment of the text.

Respond with ONLY valid JSON — no preamble, no markdown fences:
{"label": "bullish"|"bearish"|"neutral", "score": 0.0-1.0, "reasoning": "one sentence"}

Definitions:
- bullish: net positive for risk assets, economic growth, or equity markets
- bearish: net negative — contraction, crisis, or systemic stress signals
- neutral: mixed signals, informational, or not directly market-relevant
- score: your confidence in the label (≥0.7 = high, 0.4–0.7 = medium, <0.4 = low)"""


def _classify_item(title: str, summary: str | None, why: str | None) -> dict[str, Any]:
    text = (title or "")[:200]
    if summary:
        text += "\n" + summary[:400]
    if why:
        text += "\nWhy it matters: " + why[:200]

    url = settings.mlx_server_url.rstrip("/") + "/v1/chat/completions"
    try:
        with mlx_serialize():   # take turns on the shared MLX server (see digest_core)
            r = requests.post(url, json={
                "model": settings.mlx_model,
                "messages": [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user",   "content": text},
                ],
                "max_tokens": 120,
                "temperature": 0.1,
                "chat_template_kwargs": {"enable_thinking": False},
            }, timeout=settings.summarizer_timeout_sec)
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if model includes them despite instructions
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
        if m:
            raw = m.group(1)
        data = json.loads(raw)
        label = str(data.get("label", "neutral")).lower()
        if label not in ("bullish", "bearish", "neutral"):
            label = "neutral"
        score = float(data.get("score", 0.5))
        score = max(0.0, min(1.0, score))
        return {"label": label, "score": score}
    except Exception as exc:
        logger.debug("sentiment classify failed: %s", exc)
        return {"label": "neutral", "score": 0.5}


def run_sentiment(limit: int = 200) -> dict[str, int]:
    """Classify sentiment on kept+summarized items. Returns counts."""
    rows = db.items_needing_sentiment(limit=limit)
    counts = {"processed": 0, "succeeded": 0, "failed": 0}
    for row in rows:
        counts["processed"] += 1
        try:
            result = _classify_item(
                title=row["title"] or "",
                summary=row["summary"],
                why=row["why_it_matters"],
            )
            db.update_sentiment(row["id"], result["label"], result["score"])
            counts["succeeded"] += 1
        except Exception as exc:
            logger.warning("sentiment: item %s failed: %s", row["id"], exc)
            counts["failed"] += 1
    return counts
