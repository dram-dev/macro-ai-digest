"""Blog essay agent — weekly opinionated essay from raw digest signals.

Reads the week's highest-scored items *before* summarization (raw source content),
identifies dominant themes and connection threads, then asks Claude to write a
~1200-word third-person analytical essay with a specific, defensible thesis.

Pipeline position: after triage scoring, independent of the daily summarizer.
Output: <vault>/80 Digest/Essays/YYYY-MM-DD.md
Trigger: `digest essay` CLI command or launchd Saturday 09:00 local.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import html as _html_mod
from html.parser import HTMLParser

import yaml

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)

# ── System prompt ──────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a sharp, opinionated analyst-columnist writing for an elite macro-AI research publication. Your audience: senior portfolio managers, AI researchers, and technologists who expect precision, contrarianism, and a point of view that holds up to scrutiny.

Voice rules:
- Third person throughout. No "I", "we", or "our". Use "markets", "the data", "evidence", "analysts", "observers".
- Open with a clear, falsifiable thesis — not a question, a statement. Take a real position.
- Be willing to name what is wrong, overpriced, overhyped, or dangerously underappreciated. Name it.
- Cite specific numbers, organisations, people, and dates when the source material supports it.
- Write as if you will be held accountable for this take in six months.
- No hedge-word padding: cut "could potentially", "might possibly", "some argue that". State it.
- Every paragraph must follow from the prior one as part of a tight argument.
- Tension is good. Acknowledge the strongest counter-argument, then dismantle it with evidence.

Format rules:
- Title: provocative, specific, under 12 words. Not a question. No colon required.
- Hook (first paragraph, ~100 words): state the core thesis and why it is urgent right now.
- Body: 3–4 sections with ## headers, ~250 words each. Build the argument, stress-test it, resolve it.
- Conclusion (final paragraph, ~100 words): what to watch next, what changes if you are right.
- Target: approximately 1200 words total.
- Write in Markdown. Section headers use ##. No bullet lists inside the body — write in prose.
- Do not write a "Sources" or "References" section.

Return ONLY the essay in Markdown — title as # heading, body, conclusion. No preamble, no meta-commentary, no explanation of your choices."""

# ── Topic display labels (mirrors obsidian.py) ─────────────────────────

_TOPIC_LABELS: dict[str, str] = {
    "fed_markets":      "Fed & Markets",
    "china":            "China",
    "ai_thinkers":      "AI Thinkers",
    "ai_capex":         "AI Capex",
    "ai_business_apps": "AI Business Apps",
    "ai_semis":         "AI & Semis",
    "data_viz":         "Data Viz",
    "other":            "Other",
}

_CONTENT_CHARS = 700  # raw content chars per item in the prompt


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(p.strip() for p in self._parts if p.strip())


def _strip_html(text: str) -> str:
    if "<" not in text:
        return text
    stripper = _HTMLStripper()
    try:
        stripper.feed(_html_mod.unescape(text))
        return stripper.get_text() or text
    except Exception:
        return re.sub(r"<[^>]+>", " ", text)


# ── Prompt construction ────────────────────────────────────────────────

def _week_bounds(ref: date) -> tuple[date, date]:
    monday = ref - timedelta(days=ref.weekday())
    return monday, monday + timedelta(days=6)


def _topic_heat(rows: list[sqlite3.Row]) -> str:
    counts: dict[str, int] = defaultdict(int)
    scores: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        slug = row["topic"] or "other"
        counts[slug] += 1
        if row["triage_score"] is not None:
            scores[slug].append(float(row["triage_score"]))
    lines = ["Topic distribution this week:"]
    for slug, count in sorted(counts.items(), key=lambda x: -x[1]):
        avg = sum(scores[slug]) / len(scores[slug]) if scores[slug] else 0.0
        lines.append(f"  {_TOPIC_LABELS.get(slug, slug)}: {count} items, avg signal {avg:.2f}")
    return "\n".join(lines)


def _format_item(row: sqlite3.Row, rank: int) -> str:
    content = _strip_html((row["content"] or "").strip())
    if len(content) > _CONTENT_CHARS:
        content = content[:_CONTENT_CHARS] + "…"
    slug    = row["topic"] or "other"
    parts   = [
        f"[Item {rank} | score={float(row['triage_score'] or 0):.2f} | {_TOPIC_LABELS.get(slug, slug)}]",
        f"Title: {(row['title'] or '(untitled)').strip()}",
        f"Source: {row['source'] or '?'}" + (f" | By: {row['author']}" if row["author"] else ""),
        f"Date: {(row['published_at'] or row['ingested_at'] or '')[:10]}",
    ]
    if content:
        parts.append(f"Excerpt:\n{content}")
    return "\n".join(parts)


def _format_threads(threads: list[dict]) -> str:
    if not threads:
        return ""
    deduped: list[dict] = []
    seen: set[str] = set()
    for t in threads:
        theme = (t.get("theme") or "").strip()
        if theme and theme not in seen:
            seen.add(theme)
            deduped.append(t)
    if not deduped:
        return ""
    lines = [f"Pre-identified cross-item narrative threads ({len(deduped)} unique):"]
    for t in deduped[:8]:
        theme   = (t.get("theme") or "").strip()
        insight = (t.get("insight") or "").strip()
        lines.append(f"\n  ▸ {theme}")
        if insight:
            lines.append(f"    {insight[:350]}")
    return "\n".join(lines)


def _build_prompt(
    week_label: str,
    rows: list[sqlite3.Row],
    threads: list[dict],
    regime_framing: str,
) -> str:
    top = rows[:35]
    parts: list[str] = [f"Week: {week_label}"]
    if regime_framing:
        parts.append(f"Current macro regime: {regime_framing}")
    parts.append(
        f"Total high-signal items this week: {len(rows)} "
        f"(top {len(top)} shown below by signal score)"
    )
    parts.append("")
    parts.append(_topic_heat(rows))

    thread_text = _format_threads(threads)
    if thread_text:
        parts.append("")
        parts.append(thread_text)

    parts.append("")
    parts.append("=" * 64)
    parts.append("SOURCE MATERIAL (raw content — pre-summarization):")
    parts.append("")
    for i, row in enumerate(top, 1):
        parts.append(_format_item(row, i))
        parts.append("")

    parts.append("=" * 64)
    parts.append(
        "Instructions: survey the material above, identify the single most compelling "
        "angle — the intersection that reveals something markets, policymakers, or the "
        "consensus is missing or mispricing. Take that angle and build a ~1200-word essay "
        "around it. Do not recap every item; synthesise and argue."
    )
    return "\n".join(parts)


# ── Claude call ────────────────────────────────────────────────────────

def _call_claude(prompt: str) -> str:
    cmd = ["claude", "-p", "--model", settings.summarizer_model, "--output-format", "json"]
    result = subprocess.run(
        cmd,
        input=f"{SYSTEM_PROMPT}\n\n{prompt}",
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"claude exit {result.returncode}: {result.stderr.strip()[:400]}")
    try:
        envelope = json.loads(result.stdout)
        return envelope.get("result") or envelope.get("response") or result.stdout
    except json.JSONDecodeError:
        return result.stdout


# ── Obsidian writer ────────────────────────────────────────────────────

def _extract_title(essay_md: str) -> str:
    m = re.match(r"^#\s+(.+)", essay_md.strip(), re.MULTILINE)
    return m.group(1).strip() if m else "Weekly Essay"


def _word_count(text: str) -> int:
    return len(text.split())


def _write_essay(
    essay_md: str,
    week_iso: str,
    week_label: str,
    source_count: int,
    paths: Any,  # obsidian.Paths
) -> Path:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title = _extract_title(essay_md)
    wc    = _word_count(essay_md)

    front = {
        "date":         today,
        "kind":         "digest-essay",
        "week":         week_iso,
        "title":        title,
        "word_count":   wc,
        "source_items": source_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    essay_dir = paths.digest_root / "Essays"
    essay_dir.mkdir(parents=True, exist_ok=True)
    target = essay_dir / f"{today}.md"

    content = (
        "---\n"
        + yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip()
        + "\n---\n\n"
        + essay_md.strip()
        + f"\n\n---\n*Generated from {source_count} signal items for {week_label}.*\n"
    )
    target.write_text(content, encoding="utf-8")
    return target


# ── Public entry point ─────────────────────────────────────────────────

def generate_essay(ref_date: date | None = None) -> dict[str, Any]:
    """Generate a weekly opinionated essay for the week containing ref_date.

    Reads raw item content (not AI summaries) from the highest-scored kept items
    plus cross-item connection threads and macro regime context.

    Returns dict: {path, week, word_count, source_items}.
    """
    from digest.obsidian import Paths

    if ref_date is None:
        ref_date = datetime.now(timezone.utc).date()

    monday, sunday = _week_bounds(ref_date)
    week_iso   = monday.strftime("%G-W%V")
    week_label = f"{week_iso} ({monday.isoformat()} – {sunday.isoformat()})"

    rows = db.items_for_essay(
        start_iso=monday.isoformat(),
        end_iso=sunday.isoformat(),
    )
    if not rows:
        raise RuntimeError(f"No kept items found for week {week_label}")

    threads = db.connections_for_range(monday.isoformat(), sunday.isoformat())

    regime_framing = ""
    try:
        from digest.macro_regime import compute_regime
        regime_framing = compute_regime().framing
    except Exception as exc:
        logger.warning("essay: regime fetch failed: %s", exc)

    prompt = _build_prompt(week_label, rows, threads, regime_framing)
    logger.info(
        "essay: generating for %s — %d items, %d threads, prompt ~%d chars",
        week_iso, len(rows), len(threads), len(prompt),
    )

    essay_md = _call_claude(prompt)

    if not essay_md or len(essay_md.split()) < 100:
        raise RuntimeError(
            f"essay: Claude returned unexpectedly short response ({len(essay_md)} chars)"
        )

    paths = Paths.resolve()
    paths.ensure()
    path = _write_essay(essay_md, week_iso, week_label, len(rows), paths)
    wc   = _word_count(essay_md)
    logger.info("essay: wrote %s (%d words, %d source items)", path.name, wc, len(rows))

    # Scorecard intake — essays are the richest source of falsifiable calls
    try:
        from digest.predictions import extract_predictions
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        extract_predictions("essay", today, essay_md, made_on=today)
    except Exception as exc:  # noqa: BLE001
        logger.warning("essay: prediction extraction failed: %s", exc)

    return {
        "path":         str(path),
        "week":         week_iso,
        "word_count":   wc,
        "source_items": len(rows),
    }
