"""Multi-agent thesis debate — bull vs bear vs macro synthesis (Feature 10).

Three LLM agents (all backed by the MLX summarizer) debate the week's top
signals and produce a structured markdown note:
  Bull Agent   → bullish investment thesis
  Bear Agent   → bearish risk thesis
  Macro Agent  → synthesis + regime-aware positioning

Writes to: <vault>/80 Digest/Debate/YYYY-MM-DD.md
Run via: digest debate
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import requests

from digest import db
from digest.config import settings
from digest_core.summarize.backends import mlx_serialize

logger = logging.getLogger(__name__)

_BULL_SYSTEM = (
    "You are a bullish equity analyst. Argue the bull case clearly and specifically. "
    "Support your thesis with the actual signals provided. Be concise — maximum 280 words."
)
_BEAR_SYSTEM = (
    "You are a bearish risk analyst. Argue the bear case clearly and specifically. "
    "Identify structural vulnerabilities and overpriced risks. "
    "Use the actual signals provided. Be concise — maximum 280 words."
)
_SYNTHESIS_SYSTEM = (
    "You are a senior macro strategist with 20 years experience. "
    "Synthesize the bull and bear views into an actionable framework. "
    "Be direct. Maximum 240 words."
)

_BULL_USER = """\
Signals this week:
{signals}

Macro regime: {regime}

Construct the strongest BULLISH case. What are the key upside drivers?
Why are bearish concerns overblown? Be specific to the signals above."""

_BEAR_USER = """\
Signals this week:
{signals}

Macro regime: {regime}

Construct the strongest BEARISH case. What risks is the market underpricing?
What could go wrong despite the apparent positives? Be specific to the signals above."""

_SYNTHESIS_USER = """\
Bull thesis:
{bull}

Bear thesis:
{bear}

Macro regime: {regime}

Synthesize both views into three sections:
1. **Consensus**: Where do both sides agree? (shared risks)
2. **Swing factor**: What single factor determines who wins?
3. **Positioning**: Given the regime, give a concrete directional call — \
overweight/underweight/neutral on which sectors/assets and why. \
One clear, actionable recommendation."""


def _mlx(system: str, user: str, max_tokens: int = 800) -> str:
    url = settings.mlx_server_url.rstrip("/") + "/v1/chat/completions"
    with mlx_serialize():   # take turns on the shared MLX server (see digest_core)
        r = requests.post(url, json={
            "model":    settings.mlx_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "max_tokens":             max_tokens,
            "temperature":            0.5,
            "chat_template_kwargs": {"enable_thinking": False},
        }, timeout=settings.summarizer_timeout_sec * 2)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _signal_digest(rows: list) -> str:
    lines = []
    for i, row in enumerate(rows[:20], 1):
        title   = (row["title"] or "")[:100]
        topic   = row["topic"] or "other"
        score   = float(row["triage_score"] or 0)
        sent    = row["sentiment_label"] or "neutral"
        # Extract z-score for quant sources
        try:
            meta  = json.loads(row["metadata_json"] or "{}")
            z     = meta.get("z_score")
            z_str = f" z={z:+.2f}σ" if z is not None else ""
        except Exception:
            z_str = ""
        lines.append(f"{i}. [{topic}|{sent}] {title} (score={score:.2f}{z_str})")
    return "\n".join(lines) or "(no signals)"


def _signal_index(rows: list) -> list[str]:
    """Numbered, linked index matching _signal_digest's enumeration — the
    agents cite "(Signal 7)", so the note must resolve those references."""
    lines = [
        "## 📑 Signal Index",
        "",
        '_The cases above cite "Signal N" — the numbered signals they debated:_',
        "",
    ]
    for i, row in enumerate(rows[:20], 1):
        title = (
            (row["title"] or "?").replace("\n", " ")
            .replace("[", "(").replace("]", ")")[:100]
        )
        link  = f"[{title}]({row['url']})" if row["url"] else title
        topic = row["topic"] or "other"
        score = float(row["triage_score"] or 0)
        lines.append(f"{i}. {link} · `{topic}` · ⭐ {score:.2f} · `#{row['id']}`")
    return lines


def _build_stats(rows: list) -> str:
    """Return a brief statistics block: sentiment split + top entities."""
    sent_counts: Counter = Counter()
    entity_counts: Counter = Counter()
    for row in rows:
        label = row["sentiment_label"] or "neutral"
        sent_counts[label] += 1
        try:
            entities = json.loads(row["entities_json"] or "[]")
            for e in entities:
                name = e.get("name") or ""
                if name:
                    entity_counts[name] += 1
        except Exception:
            pass

    total = sum(sent_counts.values()) or 1
    sent_line = (
        f"Sentiment split: 🟢 bullish {sent_counts['bullish']} "
        f"🔴 bearish {sent_counts['bearish']} "
        f"⚪ neutral {sent_counts['neutral']} "
        f"(of {total} items)"
    )
    top_entities = ", ".join(
        f"{name} ({n})" for name, n in entity_counts.most_common(8)
    )
    entity_line = f"Top entities: {top_entities}" if top_entities else ""
    return "\n".join(filter(None, [sent_line, entity_line]))


def generate_debate(ref_date: date | None = None) -> dict:
    """Run bull/bear/synthesis debate and write Obsidian note."""
    today  = ref_date or date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)

    rows = db.items_for_week(monday.isoformat(), sunday.isoformat())
    if not rows:
        raise ValueError(f"No items for week of {monday.isoformat()} — run pipeline first")

    digest_str = _signal_digest(rows)
    stats_str  = _build_stats(rows)

    regime_row     = db.get_latest_regime()
    regime_str     = regime_row["regime"].replace("_", " ").title() if regime_row else "Unknown"
    regime_narr    = regime_row["narrative"] if regime_row else ""
    regime_context = f"{regime_str} — {regime_narr}" if regime_narr else regime_str

    logger.info("debate: week=%s regime=%s items=%d", monday.isoformat(), regime_str, len(rows))

    context_block = f"{stats_str}\n\nSignals:\n{digest_str}"
    bull = _mlx(_BULL_SYSTEM, _BULL_USER.format(signals=context_block, regime=regime_context))
    bear = _mlx(_BEAR_SYSTEM, _BEAR_USER.format(signals=context_block, regime=regime_context))
    synthesis = _mlx(
        _SYNTHESIS_SYSTEM,
        _SYNTHESIS_USER.format(bull=bull, bear=bear, regime=regime_context),
    )

    vault      = Path(settings.obsidian_vault_path).expanduser()
    debate_dir = vault / settings.obsidian_digest_dir / "Debate"
    debate_dir.mkdir(parents=True, exist_ok=True)

    sent_counts: Counter = Counter(row["sentiment_label"] or "neutral" for row in rows)

    lines = [
        "---",
        f"date: {today.isoformat()}",
        f"week: {monday.isoformat()}",
        f"regime: {regime_str}",
        f"items_reviewed: {len(rows)}",
        f"bullish: {sent_counts['bullish']}",
        f"bearish: {sent_counts['bearish']}",
        f"neutral: {sent_counts['neutral']}",
        "---",
        "",
        f"# 🥊 Thesis Debate — Week of {monday.isoformat()}",
        "",
        f"> [!info] Macro Regime: **{regime_str}**",
        f"> {regime_narr}" if regime_narr else "",
        f"> {stats_str.replace(chr(10), '  ')}",
        "",
        "---",
        "",
        "## 🟢 Bull Case",
        "",
        bull,
        "",
        "---",
        "",
        "## 🔴 Bear Case",
        "",
        bear,
        "",
        "---",
        "",
        "## ⚖️ Macro Strategist Synthesis",
        "",
        synthesis,
        "",
        "---",
        "",
        *_signal_index(rows),
        "",
        "---",
        "",
        f"*Generated from {len(rows)} signals for {monday.isoformat()} — {sunday.isoformat()}*",
    ]

    path = debate_dir / f"{today.isoformat()}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("debate: wrote %s", path.name)

    # Scorecard intake — the synthesis carries the house positioning call
    try:
        from digest.predictions import extract_predictions
        extract_predictions("debate", today.isoformat(), synthesis, made_on=today.isoformat())
    except Exception as exc:  # noqa: BLE001
        logger.warning("debate: prediction extraction failed: %s", exc)

    return {
        "path":   str(path),
        "week":   monday.isoformat(),
        "regime": regime_str,
        "items":  len(rows),
    }
