"""Mobile-first daily Brief — the ~60-line front page of the digest.

Writes `Brief/<date> Brief.md`: regime one-liner, the day's top signals
(score-ranked with a per-topic cap so FRED prints don't crowd out prose),
connection threads, a scoreboard of signals that just resolved, and the
next week of calendar events. Links into the full daily note for depth.

Top-signal items keep the seeded Claude chat link; everything else in the
vault carries a plain `#id` ref (see obsidian.py) to keep files light for
mobile reading.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from digest import db
from digest.obsidian import (
    TOPIC_CALLOUT,
    Paths,
    _chat_link,
    _title_display,
    storyline_note_name,
    topic_label,
)
from digest_core.obsidian.render import row_get as _row_get, safe as _safe

logger = logging.getLogger(__name__)

TOP_PICKS = 5
PER_TOPIC_CAP = 2
SCOREBOARD_CAP = 8
EVENTS_AHEAD_DAYS = 7
EVENTS_CAP = 6

_OUTCOME_BADGE = {"confirmed": "✅", "contradicted": "❌", "neutral": "⚖️"}


def _top_picks(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Top items by triage score, at most PER_TOPIC_CAP per topic."""
    ranked = sorted(
        rows,
        key=lambda r: (r["triage_score"] is not None, r["triage_score"] or 0.0),
        reverse=True,
    )
    picks: list[sqlite3.Row] = []
    per_topic: dict[str, int] = {}
    for row in ranked:
        slug = row["topic"] or "other"
        if per_topic.get(slug, 0) >= PER_TOPIC_CAP:
            continue
        picks.append(row)
        per_topic[slug] = per_topic.get(slug, 0) + 1
        if len(picks) >= TOP_PICKS:
            break
    return picks


def _render_pick(row: sqlite3.Row) -> list[str]:
    """One top signal: title, slim meta line (with chat link), the stake."""
    title = _title_display(_safe(row["title"]) or "(untitled)")
    url = _safe(row["url"])
    slug = row["topic"] or "other"
    why = _safe(row["why_it_matters"]) or _safe(row["summary"])
    callout = TOPIC_CALLOUT.get(slug, "note")

    heading = f"> [!{callout}]+ [{title}]({url})" if url else f"> [!{callout}]+ {title}"
    meta = [f"`{topic_label(slug)}`"]
    score = _row_get(row, "triage_score")
    if score is not None:
        try:
            meta.append(f"`⭐ {float(score):.2f}`")
        except (TypeError, ValueError):
            pass
    if _safe(row["source"]):
        meta.append(_safe(row["source"]))
    meta.append(_chat_link(row))

    lines = [heading, "> " + " · ".join(meta)]
    if why:
        lines += [">", f"> {why}"]
    return lines


def render_brief_note(date_iso: str) -> tuple[str, int]:
    """Build the Brief markdown. Returns (text, number of top picks)."""
    bundle = db.items_for_publish(date_iso)
    summarized = bundle["summarized"]
    kept_unsum = bundle["kept_unsummarized"]
    picks = _top_picks(summarized)

    front = {
        "date": date_iso,
        "kind": "digest-brief",
        "top_picks": len(picks),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    lines: list[str] = ["---", yaml.safe_dump(front, sort_keys=False).strip(), "---", ""]
    lines.append(f"# ⚡ Brief — {date_iso}")
    lines.append("")

    # ── Regime one-liner ─────────────────────────────────────────────
    try:
        regime_row = db.get_latest_regime()
        if regime_row:
            label = (regime_row["regime"] or "").replace("_", " ").title()
            week = regime_row["week"] or ""
            narrative = regime_row["narrative"] or ""
            lines.append(f"> [!info] 🌐 **{label}** *(as of {week})*")
            if narrative:
                lines.append(f"> {narrative}")
            lines.append("")
    except Exception:
        pass

    n_total = len(summarized) + len(kept_unsum)
    lines.append(
        f"_{len(summarized)} summarized + {len(kept_unsum)} kept today — "
        f"full detail in [[{date_iso}]]._"
    )
    lines.append("")

    if not n_total:
        lines.append("_No items kept by triage on this date._")
        return "\n".join(lines).rstrip() + "\n", 0

    # ── Top signals ──────────────────────────────────────────────────
    if picks:
        lines.append("## 🎯 Top Signals")
        lines.append("")
        for row in picks:
            lines.extend(_render_pick(row))
            lines.append("")

    # ── Connection threads ───────────────────────────────────────────
    threads = db.get_connections(date_iso)
    if threads:
        lines.append("## 🔗 Connection Threads")
        lines.append("")
        for thread in threads:
            theme = (thread.get("theme") or "").strip()
            insight = (thread.get("insight") or "").strip()
            ids = thread.get("item_ids") or []
            if not theme:
                continue
            lines.append(f"> [!abstract]+ 🔗 {theme}")
            if insight:
                lines.append(f"> {insight}")
            if ids:
                lines.append("> — " + " · ".join(f"`#{i}`" for i in ids))
            lines.append("")

    # ── Storylines that moved today ──────────────────────────────────
    try:
        movers = db.storylines_moved_on(date_iso)
    except Exception:
        movers = []
    if movers:
        lines.append("## 📖 Storylines")
        lines.append("")
        for m in movers:
            lines.append(f"- **[[{storyline_note_name(m['name'])}]]** — {m['delta']}")
        lines.append("")

    # ── Scoreboard: signals that just resolved ───────────────────────
    try:
        resolved = db.recently_resolved_outcomes(hours=36)
    except Exception:
        resolved = []
    if resolved:
        lines.append("## 🔁 Scoreboard — signals resolved since yesterday")
        lines.append("")
        for row in resolved[:SCOREBOARD_CAP]:
            badge = _OUTCOME_BADGE.get(row["outcome"], "⚖️")
            title = _title_display(_safe(row["title"]) or "(untitled)")
            z_move = ""
            if row["original_z"] is not None and row["followup_z"] is not None:
                z_move = f" (z {row['original_z']:+.2f} → {row['followup_z']:+.2f})"
            lines.append(
                f"- {badge} **{row['outcome']}**{z_move} — {title} `#{row['item_id']}`"
            )
        lines.append("")

    # ── Upcoming events ──────────────────────────────────────────────
    try:
        events = db.get_upcoming_events(days_ahead=EVENTS_AHEAD_DAYS)
    except Exception:
        events = []
    if events:
        lines.append("## 📅 Coming Up")
        lines.append("")
        for ev in events[:EVENTS_CAP]:
            when = ev["event_date"]
            try:
                when = f"{when} ({date.fromisoformat(when).strftime('%a')})"
            except (TypeError, ValueError):
                pass
            label = _safe(ev["title"])
            if ev["symbol"]:
                label += f" · {ev['symbol']}"
            lines.append(f"- `{when}` — {label}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n", len(picks)


def write_brief_note(date_iso: str, paths: Paths) -> tuple[Path, int]:
    """Write the Brief note. Returns (path_written, num_top_picks).

    Filename is '<date> Brief.md' (not bare '<date>.md') so `[[date]]`
    wikilinks keep resolving unambiguously to the Daily note.
    """
    text, n_picks = render_brief_note(date_iso)
    target = paths.brief_dir / f"{date_iso} Brief.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target, n_picks
