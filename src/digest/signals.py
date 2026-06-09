"""Signal leaderboard — daily all-time ranked view of the digest's highest-signal items.

Writes three Obsidian markdown files under 80 Digest/Signal/:
  High.md    — signal_score ≥ 0.72
  Medium.md  — 0.40 ≤ signal_score < 0.72
  Low.md     — signal_score < 0.40 (still summarized/kept)

Each file is a live leaderboard: fully rewritten on each run, always showing
the top-100 all-time items per tier ranked by composite signal score.
A "🆕 New — Last 24h" section highlights items ingested since yesterday.

Score formula:
  signal_score = triage_score × confidence_weight × source_multiplier × regime_weight × sentiment_weight  (clamped 0–1)

  confidence_weight:  high=1.0  medium=0.6  low=0.2
  source_multiplier:  FRED/EDGAR/CBOE/CFTC/Insider=1.2  FTD/clipped=1.1  reddit/hn=0.9
  sentiment_weight:   bullish=1.10  bearish=0.90  neutral=1.00  (only when MLX confidence ≥ 0.65)
  Tier thresholds:    High ≥ 0.72  ·  Medium 0.40–0.72  ·  Low < 0.40

Triggered by `digest signals` CLI or launchd at 08:00 CST (14:00 UTC).
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)

# ── Scoring constants ──────────────────────────────────────────────────

CONFIDENCE_WEIGHT: dict[str, float] = {"high": 1.0, "medium": 0.6, "low": 0.2}
# Sentiment nudge — applied only when MLX confidence ≥ 0.65 (avoids polluting
# items where sentiment classification was uncertain / defaulted to neutral).
SENTIMENT_MULT: dict[str, float] = {"bullish": 1.10, "bearish": 0.90, "neutral": 1.0}
SOURCE_MULTIPLIER: dict[str, float] = {
    "fred":    1.2, "edgar":   1.2, "insider": 1.2,
    "cboe":    1.2, "cftc":    1.2, "ftd":     1.1,
    "clipped": 1.1,
    "reddit":  0.9, "hn":      0.9,
}
QUANT_SOURCES = {"fred", "cboe", "cftc", "yahoo", "insider", "ftd"}

HIGH_THRESH = 0.72
MED_THRESH  = 0.40

# Regime multiplier: amplify or dampen scores based on macro environment.
# Applied on top of the base triage_score × confidence_weight × source_multiplier.
REGIME_WEIGHTS: dict[str, float] = {
    "tightening":     0.85,  # Penalize — rate-sensitive headwinds dampen signal urgency
    "on_hold":        1.00,  # Neutral
    "easing_start":   1.10,  # Boost — risk-on tailwind elevates all signals
    "recession_risk": 1.20,  # Amplify — high-alert mode, every signal counts
    "soft_landing":   1.00,  # Neutral — benign environment already priced
}

# ── Visual constants ───────────────────────────────────────────────────

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

_OUTCOME_EMOJI: dict[str, str] = {
    "confirmed":    "✅",
    "contradicted": "❌",
    "neutral":      "⚖️",
    "pending":      "⏳",
}

TIER_EMOJI  = {"high": "🔴", "medium": "🟡", "low": "🔵"}
TIER_LABEL  = {"high": "High", "medium": "Medium", "low": "Low"}
TIER_DESC   = {
    "high":   "score ≥ 0.72 — primary sources, high-confidence signals",
    "medium": "score 0.40–0.72 — reputable secondary coverage",
    "low":    "score < 0.40 — context, background, lower-confidence",
}

SOURCE_LABEL: dict[str, str] = {
    "fred": "FRED", "cboe": "CBOE", "cftc": "CFTC",
    "yahoo": "Yahoo Finance", "insider": "Insider Trades", "ftd": "Fails-to-Deliver",
    "edgar": "SEC EDGAR", "reddit": "Reddit", "hn": "Hacker News",
    "rss": "RSS", "gmail": "Gmail", "arxiv": "arXiv",
    "substack": "Substack", "clipped": "Clipped", "huggingface": "HuggingFace",
}


# ── Scoring ────────────────────────────────────────────────────────────

def signal_score(item: dict[str, Any], regime: str | None = None) -> float:
    raw_ts = item.get("triage_score")
    ts = float(raw_ts) if raw_ts is not None else 0.5
    cw = CONFIDENCE_WEIGHT.get(item.get("confidence") or "medium", 0.6)
    sm = SOURCE_MULTIPLIER.get(item.get("source") or "", 1.0)
    rw = REGIME_WEIGHTS.get(regime or "on_hold", 1.0)
    sent_score = float(item.get("sentiment_score") or 0.0)
    sw = SENTIMENT_MULT.get(item.get("sentiment_label") or "neutral", 1.0) if sent_score >= 0.65 else 1.0
    return min(1.0, ts * cw * sm * rw * sw)


def tier_for_score(score: float) -> str:
    if score >= HIGH_THRESH:
        return "high"
    if score >= MED_THRESH:
        return "medium"
    return "low"


def _parse_dt(dt_str: str | None) -> datetime:
    if not dt_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    s = dt_str.strip().replace(" ", "T").replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _parse_see_also(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        result = json.loads(raw)
        return [str(s) for s in result if s][:3]
    except Exception:
        return []


def _parse_meta(item: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(item.get("metadata_json") or "{}") or {}
    except Exception:
        return {}


# ── Prose callout rendering ────────────────────────────────────────────

def _render_prose_callout(item: dict[str, Any], score: float, is_new: bool) -> str:
    topic        = item.get("topic") or "other"
    callout_type = TOPIC_CALLOUT.get(topic, "note")
    title        = (item.get("title") or "?").replace("\n", " ").replace("|", "│")[:110]
    badge        = "🆕 " if is_new else ""
    date_str     = (item.get("published_at") or item.get("ingested_at") or "")[:10]
    url          = item.get("url") or ""
    src_label    = SOURCE_LABEL.get(item.get("source") or "", item.get("source") or "?")
    confidence   = item.get("confidence") or "medium"
    summary      = (item.get("summary") or "").strip()
    why          = (item.get("why_it_matters") or "").strip()
    see_also     = _parse_see_also(item.get("see_also"))

    # Ensemble badge: consensus score ± dispersion across 4 analyst personas
    consensus  = item.get("ensemble_consensus")
    dispersion = item.get("ensemble_dispersion")
    ens_str    = ""
    if consensus is not None:
        disp_part = f" ±{dispersion:.2f}" if dispersion is not None else ""
        ens_str   = f" · 🤝 {consensus:.2f}{disp_part}"

    # Cluster badge: narrative thread label from TF-IDF clustering
    cluster_id  = item.get("cluster_id")
    cluster_str = f" · 🏷 `{cluster_id}`" if cluster_id else ""

    link_part = f" · [→]({url})" if url else ""
    meta_line = (
        f"> `{topic}` · `{confidence}` · `⭐ {score:.2f}`{ens_str}{cluster_str}"
        f" · {src_label} · {date_str}{link_part}"
    )

    lines = [
        f"> [!{callout_type}]+ {badge}{title}",
        meta_line,
        ">",
        f"> {summary}" if summary else "> *(no summary)*",
    ]
    if why:
        lines += [">", f"> **Why it matters**: {why}"]
    if see_also:
        lines += [">", "> **See also**: " + " · ".join(f"`{s}`" for s in see_also)]

    return "\n".join(lines)


# ── Quantitative rendering ─────────────────────────────────────────────

def _z_hex(z: float | None) -> str:
    """Hex color: red spectrum for positive z, blue for negative."""
    if z is None:
        return "#94A3B8"
    abs_z = abs(z)
    if z >= 0:
        return "#B91C1C" if abs_z >= 2 else "#EF4444" if abs_z >= 1 else "#FCA5A5"
    return "#1D4ED8" if abs_z >= 2 else "#3B82F6" if abs_z >= 1 else "#93C5FD"


def _chart_block(labels: list[str], values: list[float], dataset_label: str) -> str:
    """Render a Mermaid xychart-beta bar chart (native Obsidian 1.4+, no plugin)."""
    max_abs = max((abs(v) for v in values), default=3.0)
    y_max   = max(3.0, round(max_abs + 0.5, 1))
    lbl     = "[" + ", ".join(f'"{lab}"' for lab in labels) + "]"
    dat     = "[" + ", ".join(f"{v:.2f}" for v in values) + "]"
    return (
        "```mermaid\n"
        "xychart-beta\n"
        f'    title "{dataset_label}"\n'
        f"    x-axis {lbl}\n"
        f'    y-axis "z-score" -{y_max} --> {y_max}\n'
        f"    bar {dat}\n"
        "```"
    )


def _new_flag(is_new: bool) -> str:
    return "🆕 " if is_new else ""


def _date(item: dict) -> str:
    return (item.get("published_at") or item.get("ingested_at") or "")[:10]


def _outcome_cell(item_id: int | None, outcomes: dict | None) -> str:
    if not outcomes or item_id is None:
        return "—"
    row = outcomes.get(item_id)
    if row is None:
        return "—"
    return _OUTCOME_EMOJI.get(row["outcome"], "—")


def _render_fred(
    items: list[tuple[dict, float, bool]], outcomes: dict | None = None
) -> str:
    rows = [
        "| Date | Series | Latest | Δ | z-score | Out | Score |",
        "|------|--------|--------|---|---------|-----|-------|",
    ]
    chart_labels, chart_vals = [], []
    for item, score, is_new in items:
        m     = _parse_meta(item)
        label = m.get("label") or m.get("series_id") or (item.get("title") or "")[:25]
        val   = f"{m['latest_value']:.4f}" if m.get("latest_value") is not None else "—"
        delta = f"{m['delta']:+.4f}"       if m.get("delta")        is not None else "—"
        z     = m.get("z_score")
        z_str = f"{z:+.2f}σ" if z is not None else "—"
        out   = _outcome_cell(item.get("id"), outcomes)
        rows.append(
            f"| {_new_flag(is_new)}{_date(item)} | {label} | {val} | {delta} "
            f"| {z_str} | {out} | {score:.2f} |"
        )
        if z is not None:
            chart_labels.append(label[:20])
            chart_vals.append(z)
    parts = ["\n".join(rows)]
    if chart_labels:
        parts.append(_chart_block(chart_labels, chart_vals, "z-score (FRED)"))
    return "\n\n".join(parts)


def _render_cboe(
    items: list[tuple[dict, float, bool]], outcomes: dict | None = None
) -> str:
    rows = [
        "| Date | Symbol | Value | z-score | Out | Score |",
        "|------|--------|-------|---------|-----|-------|",
    ]
    chart_labels, chart_vals = [], []
    for item, score, is_new in items:
        m     = _parse_meta(item)
        label = m.get("symbol") or (item.get("title") or "")[:25]
        val   = f"{m['value']:.2f}" if m.get("value") is not None else "—"
        z     = m.get("z_score")
        z_str = f"{z:+.2f}σ" if z is not None else "—"
        out   = _outcome_cell(item.get("id"), outcomes)
        rows.append(
            f"| {_new_flag(is_new)}{_date(item)} | {label} | {val} | {z_str} | {out} | {score:.2f} |"
        )
        if z is not None:
            chart_labels.append(label[:20])
            chart_vals.append(z)
    parts = ["\n".join(rows)]
    if chart_labels:
        parts.append(_chart_block(chart_labels, chart_vals, "z-score (CBOE)"))
    return "\n\n".join(parts)


def _render_cftc(
    items: list[tuple[dict, float, bool]], outcomes: dict | None = None
) -> str:
    rows = [
        "| Date | Contract | Net Position | Wk Chg | z-score | Out | Score |",
        "|------|----------|-------------|--------|---------|-----|-------|",
    ]
    chart_labels, chart_vals = [], []
    for item, score, is_new in items:
        m       = _parse_meta(item)
        label   = m.get("contract") or (item.get("title") or "")[:25]
        net_pos = f"{int(m['net_position']):,}" if m.get("net_position") is not None else "—"
        wk_chg  = f"{int(m['weekly_change']):+,}" if m.get("weekly_change") is not None else "—"
        z       = m.get("z_score")
        z_str   = f"{z:+.2f}σ" if z is not None else "—"
        out     = _outcome_cell(item.get("id"), outcomes)
        rows.append(
            f"| {_new_flag(is_new)}{_date(item)} | {label} | {net_pos} | {wk_chg} "
            f"| {z_str} | {out} | {score:.2f} |"
        )
        if z is not None:
            chart_labels.append(label[:20])
            chart_vals.append(z)
    parts = ["\n".join(rows)]
    if chart_labels:
        parts.append(_chart_block(chart_labels, chart_vals, "z-score (CFTC)"))
    return "\n\n".join(parts)


def _render_yahoo(
    items: list[tuple[dict, float, bool]], outcomes: dict | None = None
) -> str:
    rows = [
        "| Date | Ticker | Price | Change % | RSI-14 | Score |",
        "|------|--------|-------|----------|--------|-------|",
    ]
    for item, score, is_new in items:
        m      = _parse_meta(item)
        ticker = m.get("ticker") or (item.get("title") or "")[:15]
        price  = f"${m['price']:.2f}"       if m.get("price")      is not None else "—"
        pct    = f"{m['pct_change']:+.2f}%" if m.get("pct_change") is not None else "—"
        rsi    = f"{m['rsi14']:.1f}"        if m.get("rsi14")      is not None else "—"
        rows.append(
            f"| {_new_flag(is_new)}{_date(item)} | {ticker} | {price} | {pct} | {rsi} | {score:.2f} |"
        )
    return "\n".join(rows)


def _render_insider(
    items: list[tuple[dict, float, bool]], outcomes: dict | None = None
) -> str:
    rows = [
        "| Date | Ticker | Insider | Role | Action | Value | Score |",
        "|------|--------|---------|------|--------|-------|-------|",
    ]
    for item, score, is_new in items:
        m      = _parse_meta(item)
        ticker = m.get("ticker") or "?"
        name   = (m.get("owner") or "?")[:20]
        role   = (m.get("role") or "?")[:20]
        action = m.get("action") or "?"
        val    = f"${m['value_usd']:,.0f}" if m.get("value_usd") is not None else "—"
        rows.append(
            f"| {_new_flag(is_new)}{_date(item)} | {ticker} | {name} | {role} | {action} | {val} | {score:.2f} |"
        )
    return "\n".join(rows)


def _render_ftd(
    items: list[tuple[dict, float, bool]], outcomes: dict | None = None
) -> str:
    rows = [
        "| Date | Ticker | Shares (FTD) | Value (USD) | Score |",
        "|------|--------|-------------|-------------|-------|",
    ]
    for item, score, is_new in items:
        m      = _parse_meta(item)
        ticker = m.get("ticker") or (item.get("title") or "")[:15]
        shares = f"{int(m['qty_shares']):,}" if m.get("qty_shares") is not None else "—"
        val    = f"${m['value_usd']:,.0f}"   if m.get("value_usd") is not None else "—"
        rows.append(
            f"| {_new_flag(is_new)}{_date(item)} | {ticker} | {shares} | {val} | {score:.2f} |"
        )
    return "\n".join(rows)


_QUANT_RENDERERS = {
    "fred":    _render_fred,
    "cboe":    _render_cboe,
    "cftc":    _render_cftc,
    "yahoo":   _render_yahoo,
    "insider": _render_insider,
    "ftd":     _render_ftd,
}


def _render_quant_block(
    by_source: dict[str, list[tuple[dict, float, bool]]],
    outcomes: dict | None = None,
) -> str:
    parts = []
    for source in sorted(by_source):
        renderer = _QUANT_RENDERERS.get(source)
        if not renderer:
            continue
        src_label = SOURCE_LABEL.get(source, source.upper())
        items     = by_source[source]
        parts.append(f"#### {src_label} ({len(items)} signals)\n")
        parts.append(renderer(items, outcomes))
    return "\n\n".join(parts)


# ── Section + file rendering ───────────────────────────────────────────

def _build_section(
    header: str,
    items: list[tuple[dict, float]],
    new_ids: set[int],
    outcomes: dict | None = None,
) -> str:
    if not items:
        return f"## {header}\n\n*Nothing here yet.*\n"

    prose: list[str] = []
    quant: dict[str, list] = {}

    for item, score in items:
        is_new = (item.get("id") in new_ids)
        src    = item.get("source") or ""
        if src in QUANT_SOURCES:
            quant.setdefault(src, []).append((item, score, is_new))
        else:
            prose.append(_render_prose_callout(item, score, is_new))

    parts = [f"## {header} ({len(items)})"]
    if prose:
        parts.append("### Prose Signals\n")
        parts.append("\n\n".join(prose))
    if quant:
        parts.append("### Quantitative Signals\n")
        parts.append(_render_quant_block(quant, outcomes))

    return "\n\n".join(parts)


def _render_tier_file(
    tier: str,
    all_scored: list[tuple[dict, float]],
    now: datetime,
    top_n: int = 100,
    outcomes: dict | None = None,
) -> str:
    cutoff_24h = now - timedelta(hours=24)

    tier_items = sorted(
        [(item, score) for item, score in all_scored if tier_for_score(score) == tier],
        key=lambda x: x[1],
        reverse=True,
    )

    new_items = [
        (item, score) for item, score in tier_items
        if _parse_dt(item.get("ingested_at")) >= cutoff_24h
    ]
    new_ids = {item.get("id") for item, _ in new_items}

    historical = [
        (item, score) for item, score in tier_items[:top_n]
        if item.get("id") not in new_ids
    ]

    total = len(tier_items)
    shown = min(top_n, total - len(new_items))
    emoji = TIER_EMOJI[tier]
    label = TIER_LABEL[tier]
    desc  = TIER_DESC[tier]

    now_utc = now.strftime("%Y-%m-%d %H:%M UTC")

    # ── Track Record callout (outcomes for FRED/CBOE/CFTC signals in this tier) ──
    track_record_callout = ""
    if outcomes:
        tier_item_ids = {item.get("id") for item, _ in tier_items}
        tier_outcomes = {k: v for k, v in outcomes.items() if k in tier_item_ids}
        if tier_outcomes:
            oc = Counter(row["outcome"] for row in tier_outcomes.values())
            total_tracked = sum(oc.values())
            resolved      = total_tracked - oc.get("pending", 0)
            accuracy      = oc.get("confirmed", 0) / resolved if resolved > 0 else None
            accuracy_str  = (
                f"\n> Confirmation rate: **{accuracy:.0%}** ({resolved} resolved)"
                if accuracy is not None else ""
            )
            track_record_callout = (
                f"> [!success] Track Record — 7-day signal outcomes ({total_tracked} tracked)\n"
                f"> ✅ {oc.get('confirmed', 0)} confirmed  "
                f"❌ {oc.get('contradicted', 0)} contradicted  "
                f"⚖️ {oc.get('neutral', 0)} neutral  "
                f"⏳ {oc.get('pending', 0)} pending"
                f"{accuracy_str}"
            )

    frontmatter = (
        f"---\n"
        f"updated: {now.strftime('%Y-%m-%dT%H:%M:%S')}\n"
        f"tier: {tier}\n"
        f"total_qualifying: {total}\n"
        f"new_24h: {len(new_items)}\n"
        f"---"
    )

    header_callout = (
        f"> [!abstract] {emoji} {label} Signal — updated {now_utc}\n"
        f"> **Score** = `triage_score × confidence_weight × source_multiplier × regime_weight × sentiment_weight`\n"
        f"> {desc}\n"
        f"> Showing **{len(new_items)} new** + top **{shown}** all-time of {total} qualifying signals"
    )

    new_section     = _build_section("🆕 New — Last 24h", new_items, new_ids, outcomes)
    alltime_section = _build_section("📋 All-Time Leaders", historical, set(), outcomes)

    sections = [
        frontmatter,
        f"# {emoji} {label} Signal — All-Time Leaderboard",
        header_callout,
    ]
    if track_record_callout:
        sections.append(track_record_callout)
    sections += [
        "---",
        new_section,
        "---",
        alltime_section,
    ]

    return "\n\n".join(sections) + "\n"


# ── Public entry point ─────────────────────────────────────────────────

def write_signal_files(top_n: int = 100) -> dict[str, int]:
    """Score all summarized items, write High/Medium/Low.md. Returns per-tier counts."""
    rows = db.items_for_signals()
    if not rows:
        logger.info("signals: no summarized items — nothing to write")
        return {"high": 0, "medium": 0, "low": 0}

    now         = datetime.now(timezone.utc)
    regime_row  = db.get_latest_regime()
    regime      = regime_row["regime"] if regime_row else None
    all_scored  = [(dict(row), signal_score(dict(row), regime=regime)) for row in rows]

    # Fetch 7-day outcome data for quant signals (FRED/CBOE/CFTC)
    all_ids  = [item["id"] for item, _ in all_scored if item.get("id") is not None]
    outcomes = db.get_outcomes(all_ids) if all_ids else {}

    vault      = Path(settings.obsidian_vault_path).expanduser()
    signal_dir = vault / settings.obsidian_digest_dir / "Signal"
    signal_dir.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    for tier in ("high", "medium", "low"):
        content    = _render_tier_file(tier, all_scored, now, top_n=top_n, outcomes=outcomes)
        path       = signal_dir / f"{TIER_LABEL[tier]}.md"
        path.write_text(content, encoding="utf-8")
        tier_count = sum(1 for _, s in all_scored if tier_for_score(s) == tier)
        counts[tier] = tier_count
        logger.info("signals: wrote %s (%d items)", path.name, tier_count)

    return counts
