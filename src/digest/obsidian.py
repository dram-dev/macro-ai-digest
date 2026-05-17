"""Phase 3 — Obsidian writer.

Writes triage + summarizer output to an Obsidian vault as Markdown.

Layout:
    <vault>/<digest_dir>/
    ├── Daily/YYYY-MM-DD.md         — daily note, regenerated each run
    ├── Topics/<topic>.md           — topic archives, newest-on-top, YAML index
    └── _meta/Run Log.md            — append-only operations log

Daily notes are idempotent: rewriting the same day's note with the same data
produces byte-identical output. Topic archives use a marker-block strategy so
re-runs upsert items by ID rather than appending duplicates.

Topic display labels (e.g. "AI & Semis") differ from internal slugs
(e.g. "ai_semis"); the mapping is centralized in TOPIC_LABELS.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import yaml

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)


# ── Topic taxonomy → human labels ──────────────────────────────────────

# Internal slug → display name (file name + heading text)
TOPIC_LABELS: dict[str, str] = {
    "fed_markets":       "Fed & Markets",
    "china":             "China",
    "ai_thinkers":       "AI Thinkers",
    "ai_capex":          "AI Capex",
    "ai_business_apps":  "AI Business Apps",
    "ai_semis":          "AI & Semis",
    "data_viz":          "Data Viz Ideas",
    "other":             "Other",
}

# Display order in daily notes
TOPIC_ORDER = [
    "fed_markets",
    "china",
    "ai_thinkers",
    "ai_capex",
    "ai_business_apps",
    "ai_semis",
    "data_viz",
    "other",
]


def topic_label(slug: str) -> str:
    return TOPIC_LABELS.get(slug, slug.replace("_", " ").title())


def topic_filename(slug: str) -> str:
    """Topic archive filename — uses display label so the wikilink reads naturally."""
    return f"{topic_label(slug)}.md"


# ── Path resolution ────────────────────────────────────────────────────


@dataclass
class Paths:
    vault: Path
    digest_root: Path
    daily_dir: Path
    topics_dir: Path
    weekly_dir: Path
    meta_dir: Path

    @classmethod
    def resolve(cls) -> "Paths":
        if not settings.obsidian_vault_path:
            raise RuntimeError(
                "OBSIDIAN_VAULT_PATH is not set in .env. "
                "Set it to the absolute vault path (e.g. "
                "'/Users/you/Documents/Obsidian Vault/vault_build')."
            )
        vault = Path(settings.obsidian_vault_path).expanduser()
        if not vault.exists():
            raise RuntimeError(f"Obsidian vault not found at: {vault}")

        digest_root = vault / settings.obsidian_digest_dir
        return cls(
            vault=vault,
            digest_root=digest_root,
            daily_dir=digest_root / "Daily",
            topics_dir=digest_root / "Topics",
            weekly_dir=digest_root / "Weekly",
            meta_dir=digest_root / "_meta",
        )

    def ensure(self) -> None:
        for p in (self.digest_root, self.daily_dir, self.topics_dir, self.weekly_dir, self.meta_dir):
            p.mkdir(parents=True, exist_ok=True)


# ── Markdown rendering ─────────────────────────────────────────────────


def _safe(text: str | None) -> str:
    """Strip whitespace; return empty string if None."""
    return (text or "").strip()


def _wikilink(topic_slug: str) -> str:
    """[[Topic Name]] for graph navigation."""
    return f"[[{topic_label(topic_slug)}]]"


def _confidence_badge(c: str | None, score: float | None = None) -> str:
    """Colored badge for the summarizer's confidence label.

    If a numeric `score` is provided (typically the row's `triage_score`),
    it's appended to two decimal places — e.g. `🟢 high · 0.91`.
    """
    label = {"high": "🟢 high", "medium": "🟡 medium", "low": "🟠 low"}.get(
        (c or "").lower(), "—"
    )
    if score is None:
        return label
    try:
        return f"{label} · {float(score):.2f}"
    except (TypeError, ValueError):
        return label


# Max prompt length kept comfortably under typical URL length limits.
# claude.ai tolerates several KB in ?q=, but we cap to keep the link tidy.
_CHAT_PROMPT_MAX_CHARS = 4000


def _row_get(row: sqlite3.Row, key: str) -> str | None:
    """Safe sqlite3.Row accessor — returns None if column isn't present."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _chat_link(row: sqlite3.Row) -> str:
    """Build `[#id](https://claude.ai/new?q=…)` — clicking opens a new
    Claude chat seeded with this item's title, source, URL, and summary
    so you can immediately ask follow-up questions about it.
    """
    item_id = row["id"]
    title = _safe(_row_get(row, "title")) or "(untitled)"
    url = _safe(_row_get(row, "url"))
    source = _safe(_row_get(row, "source"))
    author = _safe(_row_get(row, "author"))
    published = _safe(_row_get(row, "published_at"))[:10]
    summary = _safe(_row_get(row, "summary"))
    why = _safe(_row_get(row, "why_it_matters"))

    lines = [
        "I'd like to dig deeper into this item from my macro/AI digest "
        f"(digest item #{item_id}).",
        "",
        f"Title: {title}",
    ]
    if source:
        lines.append(f"Source: {source}")
    if author:
        lines.append(f"Author: {author}")
    if published:
        lines.append(f"Published: {published}")
    if url:
        lines.append(f"URL: {url}")
    if summary:
        lines.append("")
        lines.append(f"Summary: {summary}")
    if why:
        lines.append("")
        lines.append(f"Why it matters: {why}")
    lines.append("")
    lines.append(
        "Please help me explore this further — context, second-order "
        "implications, related reading, or anything else worth knowing. "
        "Start by asking me what angle I want to focus on."
    )

    prompt = "\n".join(lines)
    if len(prompt) > _CHAT_PROMPT_MAX_CHARS:
        prompt = prompt[: _CHAT_PROMPT_MAX_CHARS - 1] + "…"

    encoded = urllib.parse.quote(prompt, safe="")
    return f"[#{item_id}](https://claude.ai/new?q={encoded})"


def _parse_see_also(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(v) for v in val]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _render_summary_item(row: sqlite3.Row) -> str:
    """Render one summarized item as a Markdown block."""
    title = _safe(row["title"]) or "(untitled)"
    url = _safe(row["url"])
    summary = _safe(row["summary"])
    why = _safe(row["why_it_matters"])
    confidence = row["confidence"]
    score = _row_get(row, "triage_score")
    see_also = _parse_see_also(row["see_also"])
    source = _safe(row["source"])
    author = _safe(row["author"])
    published = _safe(row["published_at"])[:10]

    # Heading with link if URL exists
    heading = f"### [{title}]({url})" if url else f"### {title}"

    meta_bits = []
    if source:
        meta_bits.append(source)
    if author:
        meta_bits.append(author)
    if published:
        meta_bits.append(published)
    meta_bits.append(_confidence_badge(confidence, score))
    meta_bits.append(_chat_link(row))
    meta_line = " · ".join(meta_bits)

    lines = [
        heading,
        f"*{meta_line}*",
        "",
        summary,
        "",
        f"**Why it matters:** {why}" if why else "",
    ]
    if see_also:
        lines.append("")
        lines.append("**See also:** " + ", ".join(f"_{s}_" for s in see_also))

    return "\n".join(line for line in lines if line is not None)


def _render_unsummarized_item(row: sqlite3.Row) -> str:
    """One-line bullet for kept-but-not-summarized items."""
    title = _safe(row["title"]) or "(untitled)"
    url = _safe(row["url"])
    source = _safe(row["source"]) or "?"
    score = row["triage_score"]
    score_str = f"score={score:.2f}" if score is not None else ""
    link = f"[{title}]({url})" if url else title
    parts = [f"- {link}", f"_{source}_"]
    if score_str:
        parts.append(score_str)
    parts.append(_chat_link(row))
    return "  ·  ".join(parts)


# ── Daily note ─────────────────────────────────────────────────────────


def _group_by_topic(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    """Group summarized rows by topic slug, preserving sort order within each."""
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["topic"] or "other", []).append(row)
    return groups


def render_daily_note(date_iso: str) -> tuple[str, list[int]]:
    """Build the markdown for a daily note. Returns (text, list of item IDs touched)."""
    bundle = db.items_for_publish(date_iso)
    summarized = bundle["summarized"]
    kept_unsum = bundle["kept_unsummarized"]
    item_ids = [r["id"] for r in summarized] + [r["id"] for r in kept_unsum]

    # User-clipped items (source='clipped') get their own headline section
    # above the auto-curated topic groups. They still carry a topic (so the
    # topic archives pick them up too) but don't double-render in the daily.
    clipped_rows = [r for r in summarized if (r["source"] or "") == "clipped"]
    auto_rows    = [r for r in summarized if (r["source"] or "") != "clipped"]

    front = {
        "date": date_iso,
        "kind": "digest-daily",
        "summarized_count": len(summarized),
        "clipped_count": len(clipped_rows),
        "kept_unsummarized_count": len(kept_unsum),
        "topics": sorted({r["topic"] or "other" for r in summarized}),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    lines: list[str] = ["---", yaml.safe_dump(front, sort_keys=False).strip(), "---", ""]
    lines.append(f"# Digest — {date_iso}")
    lines.append("")

    if not summarized and not kept_unsum:
        lines.append("_No items kept by triage on this date._")
        lines.append("")
        return "\n".join(lines), item_ids

    # ── Connection threads (cross-item synthesis) ─────────────────────
    threads = db.get_connections(date_iso)
    if threads:
        lines.append("## Connection Threads")
        lines.append("")
        lines.append(
            "_Cross-item patterns identified by Claude. Numbers are item IDs "
            "— click the `#id` link on any item below to open a seeded chat._"
        )
        lines.append("")
        for thread in threads:
            theme = (thread.get("theme") or "").strip()
            insight = (thread.get("insight") or "").strip()
            ids = thread.get("item_ids") or []
            id_refs = " · ".join(f"`#{i}`" for i in ids)
            if theme:
                lines.append(f"**{theme}** — {id_refs}")
                lines.append("")
            if insight:
                lines.append(insight)
                lines.append("")

    # ── Clipped-for-investigation section (always on top) ────────────
    if clipped_rows:
        lines.append("## Clipped for investigation")
        lines.append("")
        lines.append(
            "_Posts you flagged from `77_Claude_Investigate`, with applied "
            '"so what" analysis. Each carries a `#id` link that opens a '
            "Claude chat seeded with the post's context._"
        )
        lines.append("")
        for row in clipped_rows:
            lines.append(_render_summary_item(row))
            lines.append("")

    # ── Auto-curated summarized section, grouped by topic ────────────
    groups = _group_by_topic(auto_rows)
    if auto_rows:
        lines.append("## Summarized")
        lines.append("")
        for slug in TOPIC_ORDER:
            rows = groups.get(slug)
            if not rows:
                continue
            lines.append(f"## {topic_label(slug)}  &nbsp;·&nbsp; {_wikilink(slug)}")
            lines.append("")
            for row in rows:
                lines.append(_render_summary_item(row))
                lines.append("")
        # Any topics not in canonical order (shouldn't normally happen)
        leftover = [s for s in groups if s not in TOPIC_ORDER]
        for slug in sorted(leftover):
            lines.append(f"## {topic_label(slug)}  &nbsp;·&nbsp; {_wikilink(slug)}")
            lines.append("")
            for row in groups[slug]:
                lines.append(_render_summary_item(row))
                lines.append("")

    # ── Kept-unsummarized section ────────────────────────────────────
    if kept_unsum:
        lines.append("## Kept — not summarized this run")
        lines.append("")
        lines.append(
            "_These items passed triage but exceeded the summarizer cap. "
            "Sorted by triage score descending._"
        )
        lines.append("")
        for row in kept_unsum:
            lines.append(_render_unsummarized_item(row))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n", item_ids


def write_daily_note(date_iso: str, paths: Paths) -> tuple[Path, int]:
    """Write the daily note. Returns (path_written, num_items)."""
    text, item_ids = render_daily_note(date_iso)
    target = paths.daily_dir / f"{date_iso}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target, len(item_ids)


# ── Topic archives (newest-on-top, YAML index) ─────────────────────────

# Marker syntax: each item in a topic archive is wrapped in HTML comments
# with its DB id, so re-runs upsert by ID rather than duplicating.
ITEM_BEGIN = "<!-- digest:item:{id}:begin -->"
ITEM_END   = "<!-- digest:item:{id}:end -->"
INDEX_BEGIN = "<!-- digest:index:begin -->"
INDEX_END   = "<!-- digest:index:end -->"


def _render_topic_item(row: sqlite3.Row, topic_slug: str) -> str:
    """Render one item for a topic archive, wrapped in idempotency markers."""
    title = _safe(row["title"]) or "(untitled)"
    url = _safe(row["url"])
    summary = _safe(row["summary"])
    why = _safe(row["why_it_matters"])
    confidence = row["confidence"]
    score = _row_get(row, "triage_score")
    see_also = _parse_see_also(row["see_also"])
    source = _safe(row["source"])
    author = _safe(row["author"])
    ingested = _safe(row["ingested_at"])[:10]
    published = _safe(row["published_at"])[:10]

    heading = f"### [{title}]({url})" if url else f"### {title}"
    daily_link = f"[[{ingested}]]" if ingested else ""

    meta_bits = []
    if source:
        meta_bits.append(source)
    if author:
        meta_bits.append(author)
    if published:
        meta_bits.append(f"published {published}")
    if daily_link:
        meta_bits.append(f"in {daily_link}")
    meta_bits.append(_confidence_badge(confidence, score))
    meta_bits.append(_chat_link(row))
    meta_line = " · ".join(meta_bits)

    parts = [
        ITEM_BEGIN.format(id=row["id"]),
        heading,
        f"*{meta_line}*",
        "",
        summary,
        "",
        f"**Why it matters:** {why}" if why else "",
    ]
    if see_also:
        parts.append("")
        parts.append("**See also:** " + ", ".join(f"_{s}_" for s in see_also))
    parts.append(ITEM_END.format(id=row["id"]))
    return "\n".join(p for p in parts if p is not None)


def _build_index_block(rows: Iterable[sqlite3.Row]) -> str:
    """YAML index block listing every dated entry in this topic archive."""
    entries = []
    for row in rows:
        entries.append({
            "id": row["id"],
            "date": _safe(row["ingested_at"])[:10],
            "title": (_safe(row["title"]) or "(untitled)")[:120],
            "source": _safe(row["source"]),
        })
    payload = {"entries": entries}
    yaml_text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True).strip()
    return f"{INDEX_BEGIN}\n```yaml\n{yaml_text}\n```\n{INDEX_END}"


def render_topic_archive(topic_slug: str) -> tuple[str, list[int]]:
    """Render the full topic archive markdown. Returns (text, item_ids)."""
    rows = db.items_by_topic(topic_slug)
    item_ids = [r["id"] for r in rows]

    front = {
        "topic": topic_slug,
        "label": topic_label(topic_slug),
        "kind": "digest-topic-archive",
        "item_count": len(rows),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    lines: list[str] = ["---", yaml.safe_dump(front, sort_keys=False).strip(), "---", ""]
    lines.append(f"# {topic_label(topic_slug)}")
    lines.append("")
    lines.append("_Newest first. Each entry is upserted by ID; re-runs are idempotent._")
    lines.append("")
    lines.append("## Entries")
    lines.append("")

    for row in rows:
        lines.append(_render_topic_item(row, topic_slug))
        lines.append("")

    lines.append("## Index")
    lines.append("")
    lines.append(_build_index_block(rows))
    lines.append("")

    return "\n".join(lines).rstrip() + "\n", item_ids


def write_topic_archive(topic_slug: str, paths: Paths) -> tuple[Path, int]:
    text, item_ids = render_topic_archive(topic_slug)
    target = paths.topics_dir / topic_filename(topic_slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target, len(item_ids)


# ── Run log ────────────────────────────────────────────────────────────


RUN_LOG_HEADER = "# Digest Run Log\n\n_Append-only operations log._\n\n"


def append_run_log(paths: Paths, message: str) -> None:
    target = paths.meta_dir / "Run Log.md"
    if not target.exists():
        target.write_text(RUN_LOG_HEADER, encoding="utf-8")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    with target.open("a", encoding="utf-8") as fp:
        fp.write(f"- `{ts}` — {message}\n")


# ── Public entry point ────────────────────────────────────────────────


def publish(date_iso: str | None = None) -> dict[str, int | str]:
    """Write daily note + all topic archives. Stamp items as published.

    If date_iso is None, uses today (UTC).
    """
    paths = Paths.resolve()
    paths.ensure()

    if date_iso is None:
        date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_iso):
        raise ValueError(f"date_iso must be YYYY-MM-DD, got: {date_iso!r}")

    # Daily note
    daily_path, daily_count = write_daily_note(date_iso, paths)
    logger.info("obsidian: wrote daily %s (%d items)", daily_path.name, daily_count)

    # Topic archives — only those with summaries
    topic_results: list[tuple[str, Path, int]] = []
    for slug in db.topics_with_summaries():
        path, count = write_topic_archive(slug, paths)
        topic_results.append((slug, path, count))
        logger.info("obsidian: wrote topic %s (%d items)", path.name, count)

    # Stamp items in DB so we know what's been pushed (informational only)
    bundle = db.items_for_publish(date_iso)
    stamped = [r["id"] for r in bundle["summarized"]] + [
        r["id"] for r in bundle["kept_unsummarized"]
    ]
    db.mark_published(stamped)

    append_run_log(
        paths,
        f"published {date_iso}: {daily_count} items in daily, "
        f"{len(topic_results)} topic archives refreshed",
    )

    return {
        "date": date_iso,
        "daily_path": str(daily_path),
        "daily_items": daily_count,
        "topic_archives": len(topic_results),
        "items_stamped": len(stamped),
    }


# ── Weekly note ────────────────────────────────────────────────────────


def _week_bounds(ref_date: date) -> tuple[date, date]:
    """Return (monday, sunday) for the ISO week containing ref_date."""
    monday = ref_date - timedelta(days=ref_date.weekday())
    return monday, monday + timedelta(days=6)


def render_weekly_note(
    week_iso: str,
    monday: date,
    sunday: date,
    synthesis: dict,
    rows: list[sqlite3.Row],
) -> str:
    """Build the Markdown for a weekly digest note."""
    period = f"{monday.isoformat()} – {sunday.isoformat()}"
    front = {
        "week": week_iso,
        "period": period,
        "kind": "digest-weekly",
        "item_count": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    lines: list[str] = ["---", yaml.safe_dump(front, sort_keys=False).strip(), "---", ""]
    lines.append(f"# Weekly Digest — {week_iso}")
    lines.append(f"_{period}_")
    lines.append("")

    if not rows:
        lines.append("_No summarized items this week._")
        return "\n".join(lines).rstrip() + "\n"

    # ── Themes ──────────────────────────────────────────────────────
    themes = synthesis.get("themes") or []
    if themes:
        lines.append("## Themes of the Week")
        lines.append("")
        for i, t in enumerate(themes, 1):
            title = (t.get("title") or "").strip()
            desc = (t.get("description") or "").strip()
            lines.append(f"**{i}. {title}**")
            lines.append("")
            lines.append(desc)
            lines.append("")

    # ── Must-reads ──────────────────────────────────────────────────
    must_reads = synthesis.get("must_reads") or []
    if must_reads:
        # Build a lookup by item ID for quick access
        row_by_id = {r["id"]: r for r in rows}
        lines.append("## Must-Reads")
        lines.append("")
        for mr in must_reads:
            item_id = mr.get("item_id")
            reason = (mr.get("reason") or "").strip()
            row = row_by_id.get(item_id)
            if row:
                title = _safe(row["title"]) or "(untitled)"
                url = _safe(row["url"])
                link = f"[{title}]({url})" if url else title
                topic = topic_label(row["topic"] or "other")
                lines.append(f"- **{link}** _{topic}_ — {reason}")
            else:
                lines.append(f"- item #{item_id} — {reason}")
        lines.append("")

    # ── Contrarian signal ────────────────────────────────────────────
    contrarian = (synthesis.get("contrarian_signal") or "").strip()
    if contrarian:
        lines.append("## Contrarian Signal")
        lines.append("")
        lines.append(f"> {contrarian}")
        lines.append("")

    # ── Macro-AI intersection ────────────────────────────────────────
    macro_ai = (synthesis.get("macro_ai_intersection") or "").strip()
    if macro_ai:
        lines.append("## Macro-AI Intersection")
        lines.append("")
        lines.append(f"> {macro_ai}")
        lines.append("")

    # ── All items grouped by topic ───────────────────────────────────
    groups = _group_by_topic(list(rows))
    lines.append("## All Items This Week")
    lines.append("")
    for slug in TOPIC_ORDER:
        topic_rows = groups.get(slug)
        if not topic_rows:
            continue
        lines.append(f"### {topic_label(slug)}  &nbsp;·&nbsp; {_wikilink(slug)}")
        lines.append("")
        for row in topic_rows:
            lines.append(_render_summary_item(row))
            lines.append("")
    leftover = [s for s in groups if s not in TOPIC_ORDER]
    for slug in sorted(leftover):
        lines.append(f"### {topic_label(slug)}")
        lines.append("")
        for row in groups[slug]:
            lines.append(_render_summary_item(row))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def publish_weekly(date_iso: str | None = None) -> dict:
    """Generate and write the weekly digest note for the week containing date_iso.

    Args:
        date_iso: any YYYY-MM-DD within the target week. Defaults to today (UTC).

    Returns:
        dict with keys: week, path, item_count, theme_count.
    """
    from digest.weekly import synthesize_week

    if date_iso is None:
        date_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    ref = date.fromisoformat(date_iso)
    monday, sunday = _week_bounds(ref)
    week_iso = monday.strftime("%G-W%V")
    week_label = f"{week_iso} ({monday.isoformat()} – {sunday.isoformat()})"

    rows = db.items_for_week(monday.isoformat(), sunday.isoformat())
    logger.info("weekly: %d items for %s", len(rows), week_iso)

    synthesis = synthesize_week(rows, week_label) if rows else {}

    paths = Paths.resolve()
    paths.ensure()

    text = render_weekly_note(week_iso, monday, sunday, synthesis, rows)
    target = paths.weekly_dir / f"{week_iso}.md"
    target.write_text(text, encoding="utf-8")
    logger.info("obsidian: wrote weekly %s (%d items)", target.name, len(rows))

    append_run_log(
        paths,
        f"weekly {week_iso}: {len(rows)} items, {len(synthesis.get('themes') or [])} themes",
    )

    return {
        "week": week_iso,
        "path": str(target),
        "item_count": len(rows),
        "theme_count": len(synthesis.get("themes") or []),
    }
