"""Phase 3 — Obsidian writer.

Writes triage + summarizer output to an Obsidian vault as Markdown.

Layout:
    <vault>/<digest_dir>/
    ├── Brief/YYYY-MM-DD Brief.md   — mobile-first front page (brief.py)
    ├── Daily/YYYY-MM-DD.md         — daily note, regenerated each run
    ├── Topics/<topic>.md           — topic archives, newest-on-top, YAML index
    ├── Weekly/<week>.md            — weekly synthesis (+ companion "<week> Items.md")
    └── _meta/Run Log.md            — append-only operations log

Daily notes are idempotent: rewriting the same day's note with the same data
produces byte-identical output. Topic archives use a marker-block strategy so
re-runs upsert items by ID rather than appending duplicates.

Topic display labels (e.g. "AI & Semis") differ from internal slugs
(e.g. "ai_semis"); the mapping is centralized in TOPIC_LABELS.
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from digest import db
from digest.config import settings
from digest_core.obsidian.archive import build_index_block as _build_index_block
from digest_core.obsidian.paths import Paths as _CorePaths, append_run_log
from digest_core.obsidian.render import (
    chat_link,
    parse_see_also as _parse_see_also,
    row_get as _row_get,
    safe as _safe,
    wikilink,
)

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

# Maps topic slug → Obsidian callout type (colour-coded by urgency/category)
TOPIC_CALLOUT: dict[str, str] = {
    "ai_capex":         "info",
    "fed_markets":      "warning",
    "china":            "danger",
    "ai_thinkers":      "tip",
    "ai_semis":         "abstract",
    "ai_business_apps": "example",
    "data_viz":         "success",
    "other":            "note",
}

TOPIC_EMOJI: dict[str, str] = {
    "fed_markets":      "📊",
    "china":            "🇨🇳",
    "ai_thinkers":      "🧠",
    "ai_capex":         "💰",
    "ai_business_apps": "⚙️",
    "ai_semis":         "🔬",
    "data_viz":         "📈",
    "other":            "📌",
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


class Paths(_CorePaths):
    """macro vault paths — settings-driven resolve() over the core layout."""

    @property
    def brief_dir(self) -> Path:
        return self.digest_root / "Brief"

    @property
    def storylines_dir(self) -> Path:
        return self.digest_root / "Storylines"

    def ensure(self) -> None:
        super().ensure()
        self.brief_dir.mkdir(parents=True, exist_ok=True)
        self.storylines_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def resolve(cls) -> "Paths":
        if not settings.obsidian_vault_path:
            raise RuntimeError(
                "OBSIDIAN_VAULT_PATH is not set in .env. "
                "Set it to the absolute vault path (e.g. "
                "'/Users/you/Documents/Obsidian Vault/vault_build')."
            )
        return cls.for_vault(settings.obsidian_vault_path, settings.obsidian_digest_dir)


# ── Markdown rendering ─────────────────────────────────────────────────


def _wikilink(topic_slug: str) -> str:
    """[[Topic Label]] — resolve the macro display label, then format via core."""
    return wikilink(topic_label(topic_slug))


def _chat_link(row: sqlite3.Row) -> str:
    """macro chat deep-link — core builder with the macro/AI digest framing."""
    return chat_link(row, digest_name="macro/AI digest")


def _id_ref(row: sqlite3.Row) -> str:
    """Plain `#id` ref — greppable for thesis-testing, ~50x lighter than the
    URL-encoded chat link. Long-tail items use this; Brief top signals,
    clipped items, and weekly must-reads keep the full seeded chat link."""
    return f"`#{row['id']}`"


def _title_display(title: str) -> str:
    """Sanitise a title for callout headings: strip newlines, pipes (break
    tables) and square brackets (break link syntax), cap length."""
    return (
        title.replace("\n", " ").replace("|", "│")
             .replace("[", "(").replace("]", ")")[:110]
    )


def _render_summary_item(row: sqlite3.Row, *, with_chat_link: bool = False) -> str:
    """Render one summarized item as a topic-coloured Obsidian callout block."""
    title      = _safe(row["title"]) or "(untitled)"
    url        = _safe(row["url"])
    summary    = _safe(row["summary"])
    why        = _safe(row["why_it_matters"])
    confidence = row["confidence"]
    score      = _row_get(row, "triage_score")
    see_also   = _parse_see_also(row["see_also"])
    source     = _safe(row["source"])
    author     = _safe(row["author"])
    published  = _safe(row["published_at"])[:10]
    topic_slug = _safe(_row_get(row, "topic")) or "other"

    callout_type  = TOPIC_CALLOUT.get(topic_slug, "note")
    title_display = _title_display(title)
    heading = (
        f"> [!{callout_type}]+ [{title_display}]({url})" if url
        else f"> [!{callout_type}]+ {title_display}"
    )

    meta_parts = [f"`{topic_label(topic_slug)}`"]
    if confidence:
        meta_parts.append(f"`{confidence}`")
    if score is not None:
        try:
            meta_parts.append(f"`⭐ {float(score):.2f}`")
        except (TypeError, ValueError):
            pass
    if source:
        meta_parts.append(source)
    if author:
        meta_parts.append(author)
    if published:
        meta_parts.append(published)
    meta_parts.append(_chat_link(row) if with_chat_link else _id_ref(row))
    meta_line = "> " + " · ".join(meta_parts)

    lines = [
        heading,
        meta_line,
        ">",
        f"> {summary}" if summary else "> *(no summary)*",
    ]
    if why:
        lines += [">", f"> **Why it matters**: {why}"]
    if see_also:
        lines += [">", "> **See also**: " + " · ".join(f"`{s}`" for s in see_also[:3])]

    return "\n".join(lines)


def _render_unsummarized_item(row: sqlite3.Row) -> str:
    """One-line bullet for kept-but-not-summarized items."""
    title  = _safe(row["title"]) or "(untitled)"
    url    = _safe(row["url"])
    source = _safe(row["source"]) or "?"
    score  = row["triage_score"]
    link   = f"[{title}]({url})" if url else title
    parts  = [f"- {link}", f"*{source}*"]
    if score is not None:
        parts.append(f"`⭐ {score:.2f}`")
    parts.append(_id_ref(row))
    return "  ·  ".join(parts)


# ── Daily note ─────────────────────────────────────────────────────────


def _group_by_topic(rows: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    """Group summarized rows by topic slug, preserving sort order within each."""
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["topic"] or "other", []).append(row)
    return groups


def _topic_sections(rows: list[sqlite3.Row], heading_prefix: str = "##") -> list[str]:
    """Topic-grouped summary-item sections: canonical order first, leftovers after."""
    groups = _group_by_topic(rows)
    ordered = [s for s in TOPIC_ORDER if s in groups] + sorted(
        s for s in groups if s not in TOPIC_ORDER
    )
    lines: list[str] = []
    for slug in ordered:
        topic_rows = groups[slug]
        emoji = TOPIC_EMOJI.get(slug, "📌")
        n     = len(topic_rows)
        lines.append(
            f"{heading_prefix} {emoji} {topic_label(slug)}  ·  {_wikilink(slug)}"
            f"  ·  {n} item{'s' if n > 1 else ''}"
        )
        lines.append("")
        for row in topic_rows:
            lines.append(_render_summary_item(row))
            lines.append("")
    return lines


def render_daily_note(
    date_iso: str,
    market_snapshot_md: str = "",
) -> tuple[str, list[int]]:
    """Build the markdown for a daily note. Returns (text, list of item IDs touched).

    market_snapshot_md: pre-rendered ## Market Snapshot section (Obsidian Charts
    blocks + PNG embed). Pass empty string to omit the section.
    """
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
    lines.append(f"_Front page: [[{date_iso} Brief]]_")
    lines.append("")

    # ── Macro Regime callout ──────────────────────────────────────────────
    try:
        regime_row = db.get_latest_regime()
        if regime_row:
            regime_label    = (regime_row["regime"] or "").replace("_", " ").title()
            regime_narrative = regime_row["narrative"] or ""
            regime_week      = regime_row["week"] or ""
            lines.append(
                f"> [!info] 🌐 Macro Regime: **{regime_label}** *(as of {regime_week})*"
            )
            if regime_narrative:
                lines.append(f"> {regime_narrative}")
            lines.append("")
    except Exception:
        pass

    # ── Market Snapshot (charts) ─────────────────────────────────────────
    if market_snapshot_md:
        lines.append("## Market Snapshot")
        lines.append("")
        lines.append(market_snapshot_md)
        lines.append("")

    if not summarized and not kept_unsum:
        lines.append("_No items kept by triage on this date._")
        lines.append("")
        return "\n".join(lines), item_ids

    # ── Connection threads (cross-item synthesis) ─────────────────────
    threads = db.get_connections(date_iso)
    if threads:
        lines.append("## 🔗 Connection Threads")
        lines.append("")
        lines.append(
            "_Cross-item patterns identified by Claude. `#id` refs match items below._"
        )
        lines.append("")
        for thread in threads:
            theme   = (thread.get("theme") or "").strip()
            insight = (thread.get("insight") or "").strip()
            ids     = thread.get("item_ids") or []
            id_refs = " · ".join(f"`#{i}`" for i in ids)
            if theme:
                lines.append(f"> [!abstract]+ 🔗 {theme}")
                if id_refs:
                    lines.append(f"> **Items**: {id_refs}")
                if insight:
                    lines.append(">")
                    lines.append(f"> {insight}")
                lines.append("")

    # ── Clipped-for-investigation section (always on top) ────────────
    if clipped_rows:
        lines.append("## 📎 Clipped for Investigation")
        lines.append("")
        lines.append(
            "_Posts you flagged from `77_Claude_Investigate` — each `#id` link opens a Claude chat seeded with the context._"
        )
        lines.append("")
        for row in clipped_rows:
            lines.append(_render_summary_item(row, with_chat_link=True))
            lines.append("")

    # ── Auto-curated summarized section, grouped by topic ────────────
    if auto_rows:
        lines.append("## 📑 Summarized")
        lines.append("")
        lines.extend(_topic_sections(auto_rows, heading_prefix="##"))

    # ── Kept-unsummarized section ────────────────────────────────────
    if kept_unsum:
        lines.append("## 📋 Kept — Not Summarized")
        lines.append("")
        lines.append(
            "_Passed triage but exceeded the summarizer cap. Sorted by triage score descending._"
        )
        lines.append("")
        for row in kept_unsum:
            lines.append(_render_unsummarized_item(row))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n", item_ids


def write_daily_note(date_iso: str, paths: Paths) -> tuple[Path, int]:
    """Write the daily note. Returns (path_written, num_items)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_iso):
        raise ValueError(f"date_iso must be YYYY-MM-DD, got: {date_iso!r}")
    from digest.charts import build_obsidian_charts, render_png

    assets_dir = paths.digest_root / "assets"
    snapshot_parts: list[str] = []

    try:
        plugin_charts = build_obsidian_charts(date_iso)
        if plugin_charts:
            snapshot_parts.append("### Plugin Charts *(requires Obsidian Charts)*")
            snapshot_parts.append("")
            snapshot_parts.append(plugin_charts)
    except Exception as exc:
        logger.warning("charts: obsidian chart build failed: %s", exc)

    try:
        png_embed = render_png(date_iso, assets_dir)
        if png_embed:
            snapshot_parts.append("### PNG Snapshot *(always renders)*")
            snapshot_parts.append("")
            snapshot_parts.append(png_embed)
    except Exception as exc:
        logger.warning("charts: PNG render failed: %s", exc)

    market_snapshot_md = "\n".join(snapshot_parts)

    text, item_ids = render_daily_note(date_iso, market_snapshot_md=market_snapshot_md)
    target = paths.daily_dir / f"{date_iso}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target, len(item_ids)


# ── Topic archives (newest-on-top, YAML index) ─────────────────────────

# Marker syntax: each item in a topic archive is wrapped in HTML comments
# with its DB id, so re-runs upsert by ID rather than duplicating.
ITEM_BEGIN = "<!-- digest:item:{id}:begin -->"
ITEM_END   = "<!-- digest:item:{id}:end -->"
# INDEX_BEGIN/INDEX_END + the index block builder now live in
# digest_core.obsidian.archive (imported above as _build_index_block).


def _render_topic_item(row: sqlite3.Row, topic_slug: str) -> str:
    """Render one item for a topic archive as a callout block, wrapped in idempotency markers."""
    title      = _safe(row["title"]) or "(untitled)"
    url        = _safe(row["url"])
    summary    = _safe(row["summary"])
    why        = _safe(row["why_it_matters"])
    confidence = row["confidence"]
    score      = _row_get(row, "triage_score")
    see_also   = _parse_see_also(row["see_also"])
    source     = _safe(row["source"])
    author     = _safe(row["author"])
    ingested   = _safe(row["ingested_at"])[:10]
    published  = _safe(row["published_at"])[:10]

    callout_type  = TOPIC_CALLOUT.get(topic_slug, "note")
    title_display = _title_display(title)
    daily_link = f"[[{ingested}]]" if ingested else ""
    heading = (
        f"> [!{callout_type}]+ [{title_display}]({url})" if url
        else f"> [!{callout_type}]+ {title_display}"
    )

    meta_parts = []
    if source:
        meta_parts.append(source)
    if author:
        meta_parts.append(author)
    if published:
        meta_parts.append(published)
    if daily_link:
        meta_parts.append(f"in {daily_link}")
    if confidence:
        meta_parts.append(f"`{confidence}`")
    if score is not None:
        try:
            meta_parts.append(f"`⭐ {float(score):.2f}`")
        except (TypeError, ValueError):
            pass
    meta_parts.append(_id_ref(row))
    meta_line = "> " + " · ".join(meta_parts)

    parts = [
        ITEM_BEGIN.format(id=row["id"]),
        heading,
        meta_line,
        ">",
        f"> {summary}" if summary else "> *(no summary)*",
    ]
    if why:
        parts += [">", f"> **Why it matters**: {why}"]
    if see_also:
        parts += [">", "> **See also**: " + " · ".join(f"`{s}`" for s in see_also[:3])]
    parts.append(ITEM_END.format(id=row["id"]))
    return "\n".join(p for p in parts if p is not None)


def _render_archive_doc(
    topic_slug: str,
    rows: list[sqlite3.Row],
    *,
    title: str,
    note_line: str,
    rollover_section: list[str] | None = None,
    stamp_updated: bool = True,
) -> str:
    """Render one archive document (main topic file or a monthly rollover)."""
    front = {
        "topic": topic_slug,
        "label": topic_label(topic_slug),
        "kind": "digest-topic-archive",
        "item_count": len(rows),
    }
    # Rollover files omit updated_at so frozen months render byte-identically
    # and write_topic_archive can skip rewriting them.
    if stamp_updated:
        front["updated_at"] = datetime.now(timezone.utc).isoformat()

    emoji = TOPIC_EMOJI.get(topic_slug, "📌")
    lines: list[str] = ["---", yaml.safe_dump(front, sort_keys=False).strip(), "---", ""]
    lines.append(f"# {emoji} {title}")
    lines.append("")
    lines.append(note_line)
    lines.append("")
    lines.append("## Entries")
    lines.append("")

    for row in rows:
        lines.append(_render_topic_item(row, topic_slug))
        lines.append("")

    if rollover_section:
        lines.extend(rollover_section)

    lines.append("## Index")
    lines.append("")
    lines.append(_build_index_block(rows))
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _split_rollover(
    rows: list[sqlite3.Row], cap: int
) -> tuple[list[sqlite3.Row], dict[str, list[sqlite3.Row]]]:
    """Split newest-first rows into (recent, older-by-ingestion-month)."""
    if not cap or len(rows) <= cap:
        return rows, {}
    by_month: dict[str, list[sqlite3.Row]] = {}
    for row in rows[cap:]:
        month = (row["ingested_at"] or "")[:7] or "undated"
        by_month.setdefault(month, []).append(row)
    return rows[:cap], by_month


def _render_topic_docs(topic_slug: str) -> tuple[str, dict[str, str], list[int]]:
    """Render the capped main archive + frozen monthly rollover docs.

    Returns (main_text, {rollover_filename: text}, all_item_ids).
    """
    rows = db.items_by_topic(topic_slug)
    recent, by_month = _split_rollover(rows, settings.obsidian_topic_archive_cap)
    label = topic_label(topic_slug)

    rollover_docs: dict[str, str] = {}
    rollover_section: list[str] | None = None
    if by_month:
        rollover_section = ["## Older entries", ""]
        for month in sorted(by_month, reverse=True):
            mrows = by_month[month]
            rollover_section.append(f"- [[{label} {month}]] — {len(mrows)} items")
            rollover_docs[f"{label} {month}.md"] = _render_archive_doc(
                topic_slug,
                mrows,
                title=f"{label} — {month}",
                note_line="_Rolled over from the main topic archive; this file is frozen._",
                stamp_updated=False,
            )
        rollover_section.append("")

    main = _render_archive_doc(
        topic_slug,
        recent,
        title=label,
        note_line="_Newest first. Each entry is upserted by ID; re-runs are idempotent._",
        rollover_section=rollover_section,
    )
    return main, rollover_docs, [r["id"] for r in rows]


def render_topic_archive(topic_slug: str) -> tuple[str, list[int]]:
    """Render the main topic archive markdown. Returns (text, item_ids)."""
    main, _, item_ids = _render_topic_docs(topic_slug)
    return main, item_ids


def write_topic_archive(topic_slug: str, paths: Paths) -> tuple[Path, int]:
    main, rollover_docs, item_ids = _render_topic_docs(topic_slug)
    target = paths.topics_dir / topic_filename(topic_slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(main, encoding="utf-8")

    if rollover_docs:
        archive_dir = paths.topics_dir / "Archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        for fname, text in rollover_docs.items():
            path = archive_dir / fname
            # Frozen months are byte-stable — skip the write (and the Obsidian
            # sync churn) unless content actually changed.
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.write_text(text, encoding="utf-8")
    return target, len(item_ids)


# ── Storylines (Wave 2: multi-day narrative threading) ─────────────────

_STATUS_BADGE = {"active": "🟢", "dormant": "💤", "resolved": "✅"}


def storyline_note_name(name: str) -> str:
    """Note name (no .md) for a storyline — the name with filename/wikilink
    breakers stripped. Names are fixed at creation, so this is stable."""
    clean = re.sub(r'[/\\:|#^\[\]?*"<>]', "-", name)
    return re.sub(r"\s+", " ", clean).strip() or "Untitled storyline"


def render_storyline_note(story: sqlite3.Row, deltas: list[sqlite3.Row]) -> str:
    """One storyline page: current state on top, newest-first timeline below."""
    from digest.storylines import parse_delta_item_ids

    status = story["status"] or "active"
    front = {
        "slug": story["slug"],
        "kind": "digest-storyline",
        "status": status,
        "delta_count": len(deltas),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    lines: list[str] = ["---", yaml.safe_dump(front, sort_keys=False).strip(), "---", ""]
    lines.append(f"# 📖 {story['name']}")
    lines.append("")

    badge = _STATUS_BADGE.get(status, "🟢")
    moved = story["last_moved"] or "never"
    if status == "resolved":
        lines.append(f"> [!success] {badge} Resolved — last moved {moved}")
        if story["resolution"]:
            lines.append(f"> {story['resolution']}")
        lines.append(">")
        lines.append(f"> {story['state']}")
    else:
        lines.append(f"> [!abstract] {badge} Where this stands *(as of {moved})*")
        lines.append(f"> {story['state']}")
    lines.append("")

    if deltas:
        lines.append("## Timeline")
        lines.append("")
        for d in deltas:
            lines.append(f"### [[{d['date']}]]")
            lines.append("")
            lines.append(d["delta"])
            ids = parse_delta_item_ids(d["item_ids"])
            if ids:
                lines.append("")
                lines.append("— " + " · ".join(f"`#{i}`" for i in ids))
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_storylines_index(stories: list[sqlite3.Row]) -> str:
    """The Storylines index note: active first, then dormant, then resolved."""
    front = {
        "kind": "digest-storylines-index",
        "count": len(stories),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    lines: list[str] = ["---", yaml.safe_dump(front, sort_keys=False).strip(), "---", ""]
    lines.append("# 📖 Storylines")
    lines.append("")
    lines.append("_Persistent narratives tracked across days. Movers surface in the daily Brief._")
    lines.append("")
    by_status: dict[str, list[sqlite3.Row]] = {}
    for s in stories:
        by_status.setdefault(s["status"] or "active", []).append(s)
    for status, heading in (
        ("active", "🟢 Active"), ("dormant", "💤 Dormant"), ("resolved", "✅ Resolved"),
    ):
        group = by_status.get(status)
        if not group:
            continue
        lines.append(f"## {heading}")
        lines.append("")
        for s in group:
            moved = s["last_moved"] or "—"
            lines.append(f"- [[{storyline_note_name(s['name'])}]] — *last moved {moved}*")
        lines.append("")
    if not stories:
        lines.append("_No storylines tracked yet._")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_storylines(paths: Paths) -> int:
    """Write every storyline page + the index. Returns number of storylines."""
    stories = db.get_storylines(statuses=("active", "dormant", "resolved"))
    paths.storylines_dir.mkdir(parents=True, exist_ok=True)
    for story in stories:
        deltas = db.get_storyline_deltas(story["id"])
        text = render_storyline_note(story, deltas)
        target = paths.storylines_dir / f"{storyline_note_name(story['name'])}.md"
        target.write_text(text, encoding="utf-8")
    index = render_storylines_index(stories)
    (paths.storylines_dir / "Storylines.md").write_text(index, encoding="utf-8")
    return len(stories)


# append_run_log + RUN_LOG_HEADER now live in digest_core.obsidian.paths
# (imported above); call sites use append_run_log unchanged.


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

    # Brief — the mobile-first front page. Best-effort: never blocks the daily.
    brief_path = ""
    try:
        from digest.brief import write_brief_note
        bpath, n_picks = write_brief_note(date_iso, paths)
        brief_path = str(bpath)
        logger.info("obsidian: wrote brief %s (%d top picks)", bpath.name, n_picks)
    except Exception as exc:  # noqa: BLE001
        logger.warning("obsidian: brief write failed: %s", exc)

    # Storyline pages + index — best-effort like the brief
    storyline_count = 0
    try:
        storyline_count = write_storylines(paths)
        logger.info("obsidian: wrote %d storyline pages", storyline_count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("obsidian: storyline write failed: %s", exc)

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
        "brief_path": brief_path,
        "daily_items": daily_count,
        "topic_archives": len(topic_results),
        "storylines": storyline_count,
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
    regime_md: str | None = None,
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
    if regime_md:
        lines.append(regime_md)
        lines.append("")

    if not rows:
        lines.append("_No summarized items this week._")
        return "\n".join(lines).rstrip() + "\n"

    # ── Themes ──────────────────────────────────────────────────────
    themes = synthesis.get("themes") or []
    if themes:
        lines.append("## 🎯 Themes of the Week")
        lines.append("")
        for i, t in enumerate(themes, 1):
            title = (t.get("title") or "").strip()
            desc  = (t.get("description") or "").strip()
            lines.append(f"> [!tip]+ 🎯 Theme {i}: {title}")
            if desc:
                lines.append(f"> {desc}")
            lines.append("")

    # ── Must-reads ──────────────────────────────────────────────────
    must_reads = synthesis.get("must_reads") or []
    if must_reads:
        row_by_id = {r["id"]: r for r in rows}
        lines.append("## 📌 Must-Reads")
        lines.append("")
        for mr in must_reads:
            item_id = mr.get("item_id")
            reason  = (mr.get("reason") or "").strip()
            row     = row_by_id.get(item_id)
            if row:
                title       = _safe(row["title"]) or "(untitled)"
                url         = _safe(row["url"])
                slug        = row["topic"] or "other"
                link        = f"[{title}]({url})" if url else title
                topic_disp  = topic_label(slug)
                callout_t   = TOPIC_CALLOUT.get(slug, "note")
                lines.append(f"> [!{callout_t}]+ 📌 {link}")
                lines.append(f"> `{topic_disp}` · {_chat_link(row)} — {reason}")
            else:
                lines.append(f"> [!note]+ 📌 Item #{item_id}")
                lines.append(f"> {reason}")
            lines.append("")

    # ── Contrarian signal ────────────────────────────────────────────
    contrarian = (synthesis.get("contrarian_signal") or "").strip()
    if contrarian:
        lines.append("## ⚠️ Contrarian Signal")
        lines.append("")
        lines.append("> [!danger] ⚠️ Contrarian Signal")
        lines.append(f"> {contrarian}")
        lines.append("")

    # ── Macro-AI intersection ────────────────────────────────────────
    macro_ai = (synthesis.get("macro_ai_intersection") or "").strip()
    if macro_ai:
        lines.append("## 🔗 Macro-AI Intersection")
        lines.append("")
        lines.append("> [!abstract] 🔗 Macro-AI Intersection")
        lines.append(f"> {macro_ai}")
        lines.append("")

    # ── Pointer to the companion items file ──────────────────────────
    # The full item replay (~95% of the old weekly's bulk) lives in a
    # separate note so the weekly itself stays a one-sitting mobile read.
    lines.append("## 📑 All Items This Week")
    lines.append("")
    lines.append(f"All **{len(rows)}** items → [[{weekly_items_name(week_iso)}]]")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def weekly_items_name(week_iso: str) -> str:
    """Note name (no .md) of the weekly companion item archive."""
    return f"{week_iso} Items"


def render_weekly_items_note(
    week_iso: str,
    monday: date,
    sunday: date,
    rows: list[sqlite3.Row],
) -> str:
    """Companion note holding the full topic-grouped item replay for a week."""
    period = f"{monday.isoformat()} – {sunday.isoformat()}"
    front = {
        "week": week_iso,
        "period": period,
        "kind": "digest-weekly-items",
        "item_count": len(rows),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    lines: list[str] = ["---", yaml.safe_dump(front, sort_keys=False).strip(), "---", ""]
    lines.append(f"# 📑 All Items — {week_iso}")
    lines.append(f"_{period} · synthesis in [[{week_iso}]]_")
    lines.append("")
    if not rows:
        lines.append("_No summarized items this week._")
        return "\n".join(lines).rstrip() + "\n"
    lines.extend(_topic_sections(list(rows), heading_prefix="##"))
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

    # Compute macro regime — best-effort, non-blocking
    regime_md: str | None = None
    regime_framing: str = ""
    try:
        from digest.macro_regime import compute_regime
        result = compute_regime()
        regime_framing = result.framing
        regime_md = f"> [!info] Macro Regime: {result.label}\n> {result.narrative}"
    except Exception as exc:
        logger.warning("weekly: regime computation failed: %s", exc)

    storyline_context = ""
    try:
        from digest.storylines import storyline_context_for_weekly
        storyline_context = storyline_context_for_weekly()
    except Exception as exc:
        logger.warning("weekly: storyline context failed: %s", exc)

    synthesis = (
        synthesize_week(
            rows, week_label,
            regime_framing=regime_framing,
            storyline_context=storyline_context,
        )
        if rows else {}
    )

    paths = Paths.resolve()
    paths.ensure()

    text = render_weekly_note(week_iso, monday, sunday, synthesis, rows, regime_md=regime_md)
    target = paths.weekly_dir / f"{week_iso}.md"
    target.write_text(text, encoding="utf-8")
    logger.info("obsidian: wrote weekly %s (%d items)", target.name, len(rows))

    if rows:
        items_text = render_weekly_items_note(week_iso, monday, sunday, rows)
        items_target = paths.weekly_dir / f"{weekly_items_name(week_iso)}.md"
        items_target.write_text(items_text, encoding="utf-8")
        logger.info("obsidian: wrote weekly items %s", items_target.name)

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
