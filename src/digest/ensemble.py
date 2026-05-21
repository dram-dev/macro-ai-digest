"""Multi-persona ensemble scorer (Idea 3).

Runs four independent Ollama personas against each summarized item:
  macro_rates  — rates/macro analyst
  momentum     — systematic momentum/flow trader
  value        — value/fundamental analyst
  contrarian   — contrarian/tail-risk analyst

Outputs stored on items:
  ensemble_scores      — JSON {persona: score}
  ensemble_consensus   — mean of persona scores (0–1)
  ensemble_dispersion  — std dev of persona scores (high = barbell/divisive item)

High consensus + high score → "must-read" across all frameworks.
High dispersion → relevant to some perspectives, irrelevant to others.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time

import requests

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)

_OLLAMA_URL = "{host}/api/generate"
_TIMEOUT = 60

PERSONAS: dict[str, str] = {
    "macro_rates": (
        "You are a senior rates and macro analyst at a fixed-income fund. "
        "Evaluate news for actionable macro/rates positioning signal. "
        "Score 0.0–1.0: 1.0 = immediately actionable for macro/rates, 0.0 = irrelevant. "
        'Respond with ONLY valid JSON: {"score": 0.0, "reason": "max 10 words"}'
    ),
    "momentum": (
        "You are a systematic momentum and flow-of-funds trader. "
        "Evaluate news for momentum and positioning signals. "
        "Score 0.0–1.0: 1.0 = clear positioning opportunity now, 0.0 = irrelevant. "
        'Respond with ONLY valid JSON: {"score": 0.0, "reason": "max 10 words"}'
    ),
    "value": (
        "You are a value and fundamental analyst at a long-only equity fund. "
        "Evaluate news for impact on fundamental valuations. "
        "Score 0.0–1.0: 1.0 = material fundamental surprise, 0.0 = irrelevant. "
        'Respond with ONLY valid JSON: {"score": 0.0, "reason": "max 10 words"}'
    ),
    "contrarian": (
        "You are a contrarian risk analyst focused on crowded trades and tail risks. "
        "Evaluate news as a potential contrarian or tail-risk signal. "
        "Score 0.0–1.0: 1.0 = clear crowded-trade reversal or tail risk, 0.0 = irrelevant. "
        'Respond with ONLY valid JSON: {"score": 0.0, "reason": "max 10 words"}'
    ),
}

_USER_TEMPLATE = """Title: {title}
Topic: {topic}
Source: {source}

Summary: {summary}

Why it matters: {why}

JSON score:"""


def _build_prompt(item: dict) -> str:
    summary = (item.get("summary") or "").strip()
    if len(summary) > 600:
        summary = summary[:600] + "…"
    why = (item.get("why_it_matters") or "").strip()
    if len(why) > 200:
        why = why[:200] + "…"
    return _USER_TEMPLATE.format(
        title=(item.get("title") or "?")[:120],
        topic=item.get("topic") or "other",
        source=item.get("source") or "?",
        summary=summary or "(none)",
        why=why or "(none)",
    )


def _extract_score(raw: str) -> float | None:
    raw = raw.strip()
    try:
        d = json.loads(raw)
        return float(d["score"])
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(1))
            return float(d["score"])
        except Exception:
            pass
    m = re.search(r'"score"\s*:\s*([0-9.]+)', raw)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _ollama_call(prompt: str, system: str) -> str:
    url = _OLLAMA_URL.format(host=settings.ollama_host.rstrip("/"))
    payload = {
        "model": settings.ollama_model,
        "prompt": prompt,
        "system": system,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.15, "num_predict": 128, "num_ctx": 2048},
    }
    r = requests.post(url, json=payload, timeout=_TIMEOUT)
    r.raise_for_status()
    return r.json().get("response", "")


def score_item(item: dict) -> dict[str, float]:
    """Run all 4 personas against one item. Returns {persona: score} for successful calls."""
    prompt = _build_prompt(item)
    scores: dict[str, float] = {}
    for name, system in PERSONAS.items():
        try:
            raw = _ollama_call(prompt, system)
            score = _extract_score(raw)
            if score is not None:
                scores[name] = max(0.0, min(1.0, score))
        except Exception as exc:  # noqa: BLE001
            logger.warning("ensemble: persona=%s item=%s failed: %s", name, item.get("id"), exc)
    return scores


def _stats(values: list[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return mean, math.sqrt(variance)


def run_ensemble(limit: int = 200) -> dict[str, int]:
    """Score items needing ensemble evaluation. Returns counts."""
    items = db.items_needing_ensemble(limit=limit)
    if not items:
        logger.info("ensemble: nothing to score")
        return {"processed": 0, "succeeded": 0, "failed": 0}

    counts = {"processed": len(items), "succeeded": 0, "failed": 0}
    for row in items:
        item_dict = dict(row)
        t0 = time.perf_counter()
        try:
            scores = score_item(item_dict)
            if not scores:
                counts["failed"] += 1
                continue
            values = list(scores.values())
            consensus, dispersion = _stats(values)
            db.update_ensemble(
                item_id=item_dict["id"],
                scores_json=json.dumps(scores),
                consensus=round(consensus, 4),
                dispersion=round(dispersion, 4),
            )
            counts["succeeded"] += 1
            elapsed = time.perf_counter() - t0
            logger.info(
                "ensemble: id=%d consensus=%.2f disp=%.2f (%.1fs)",
                item_dict["id"], consensus, dispersion, elapsed,
            )
        except Exception as exc:  # noqa: BLE001
            counts["failed"] += 1
            logger.exception("ensemble: id=%s failed: %s", item_dict.get("id"), exc)

    return counts
