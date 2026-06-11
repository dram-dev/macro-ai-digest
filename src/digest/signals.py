"""Signal leaderboard — daily ranked view of the digest's highest-signal items.

Writes three Obsidian markdown files under 80 Digest/Signal/:
  High.md    — signal_score ≥ 0.72
  Medium.md  — 0.40 ≤ signal_score < 0.72
  Low.md     — signal_score < 0.40 (still summarized/kept)

Each file is a live leaderboard: fully rewritten on each run, showing the
top-100 items per tier from a rolling 90-day window (an all-time view just
pins stale ⭐1.00 items to the top forever). A "🆕 New — Last 24h" section
highlights items ingested since yesterday. Routine insider-sale drips (same
insider/ticker/action ≥3×, similar size) collapse into one summary row.

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

# Leaders section window — all-time froze early Clipped ⭐1.00 items on top
LEADER_WINDOW_DAYS = 90

# Insider drip collapsing: ≥3 same (ticker, insider, action) rows collapse to
# one summary line; a trade >5× the group median stays its own row so true
# anomalies (a $111M sale among $500k drips) never get buried.
DRIP_MIN_TRADES = 3
DRIP_OUTLIER_MULT = 5.0

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
# (The mermaid xychart blocks that used to accompany these tables were
# dropped: with 40+ truncated x-axis labels they were unreadable, and the
# tables carry the same data.)

def _num(value: Any) -> float | None:
    """Float value, or None for missing/NaN — keeps 'nan' out of the tables."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


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
    for item, score, is_new in items:
        m = _parse_meta(item)
        z = _num(m.get("z_score"))
        if z is None:
            continue   # a z-anomaly row without a z is noise, not signal
        label   = m.get("label") or m.get("series_id") or (item.get("title") or "")[:25]
        latest  = _num(m.get("latest_value"))
        delta_v = _num(m.get("delta"))
        val   = f"{latest:.4f}"   if latest  is not None else "—"
        delta = f"{delta_v:+.4f}" if delta_v is not None else "—"
        out   = _outcome_cell(item.get("id"), outcomes)
        rows.append(
            f"| {_new_flag(is_new)}{_date(item)} | {label} | {val} | {delta} "
            f"| {z:+.2f}σ | {out} | {score:.2f} |"
        )
    return "\n".join(rows)


def _render_cboe(
    items: list[tuple[dict, float, bool]], outcomes: dict | None = None
) -> str:
    rows = [
        "| Date | Symbol | Value | z-score | Out | Score |",
        "|------|--------|-------|---------|-----|-------|",
    ]
    for item, score, is_new in items:
        m = _parse_meta(item)
        z = _num(m.get("z_score"))
        if z is None:
            continue
        label = m.get("symbol") or (item.get("title") or "")[:25]
        value = _num(m.get("value"))
        val   = f"{value:.2f}" if value is not None else "—"
        out   = _outcome_cell(item.get("id"), outcomes)
        rows.append(
            f"| {_new_flag(is_new)}{_date(item)} | {label} | {val} | {z:+.2f}σ | {out} | {score:.2f} |"
        )
    return "\n".join(rows)


def _render_cftc(
    items: list[tuple[dict, float, bool]], outcomes: dict | None = None
) -> str:
    rows = [
        "| Date | Contract | Net Position | Wk Chg | z-score | Out | Score |",
        "|------|----------|-------------|--------|---------|-----|-------|",
    ]
    for item, score, is_new in items:
        m = _parse_meta(item)
        z = _num(m.get("z_score"))
        if z is None:
            continue
        label   = m.get("contract") or (item.get("title") or "")[:25]
        net     = _num(m.get("net_position"))
        wk      = _num(m.get("weekly_change"))
        net_pos = f"{int(net):,}" if net is not None else "—"
        wk_chg  = f"{int(wk):+,}" if wk  is not None else "—"
        out     = _outcome_cell(item.get("id"), outcomes)
        rows.append(
            f"| {_new_flag(is_new)}{_date(item)} | {label} | {net_pos} | {wk_chg} "
            f"| {z:+.2f}σ | {out} | {score:.2f} |"
        )
    return "\n".join(rows)


def _render_yahoo(
    items: list[tuple[dict, float, bool]], outcomes: dict | None = None
) -> str:
    rows = [
        "| Date | Ticker | Price | Change % | RSI-14 | Score |",
        "|------|--------|-------|----------|--------|-------|",
    ]
    for item, score, is_new in items:
        m       = _parse_meta(item)
        ticker  = m.get("ticker") or (item.get("title") or "")[:15]
        price_v = _num(m.get("price"))
        pct_v   = _num(m.get("pct_change"))
        rsi_v   = _num(m.get("rsi14"))
        if price_v is None and pct_v is None:
            continue   # "$nan / nan%" rows carry nothing
        price = f"${price_v:.2f}"  if price_v is not None else "—"
        pct   = f"{pct_v:+.2f}%"   if pct_v   is not None else "—"
        rsi   = f"{rsi_v:.1f}"     if rsi_v   is not None else "—"
        rows.append(
            f"| {_new_flag(is_new)}{_date(item)} | {ticker} | {price} | {pct} | {rsi} | {score:.2f} |"
        )
    return "\n".join(rows)


def _insider_row(
    date_str: str, is_new: bool, ticker: str, name: str, role: str,
    action: str, value: float | None, score: float, count: int = 1,
) -> str:
    if count > 1:
        action = f"{action} ×{count} — routine drip"
        val = f"${value:,.0f} total" if value is not None else "—"
    else:
        val = f"${value:,.0f}" if value is not None else "—"
    return (
        f"| {_new_flag(is_new)}{date_str} | {ticker} | {name[:20]} | {role[:20]} "
        f"| {action} | {val} | {score:.2f} |"
    )


def _render_insider(
    items: list[tuple[dict, float, bool]], outcomes: dict | None = None
) -> str:
    """Insider table with routine-drip collapsing.

    ≥DRIP_MIN_TRADES rows by the same (ticker, insider, action) collapse to a
    single date-ranged total — programmatic 10b5-1 selling is one fact, not
    twelve rows. Any trade >DRIP_OUTLIER_MULT× the group median keeps its own
    row so genuinely outsized sales never disappear into a drip line.
    """
    rows = [
        "| Date | Ticker | Insider | Role | Action | Value | Score |",
        "|------|--------|---------|------|--------|-------|-------|",
    ]
    groups: dict[tuple, list] = {}
    for item, score, is_new in items:
        m   = _parse_meta(item)
        key = (m.get("ticker") or "?", m.get("owner") or "?", m.get("action") or "?")
        groups.setdefault(key, []).append((item, score, is_new, m))

    for (ticker, name, action), entries in groups.items():
        values = sorted(
            v for v in (_num(e[3].get("value_usd")) for e in entries) if v is not None
        )
        median = values[len(values) // 2] if values else None

        outliers, routine = [], []
        for e in entries:
            v = _num(e[3].get("value_usd"))
            big = median is not None and v is not None and v > DRIP_OUTLIER_MULT * median
            (outliers if big else routine).append(e)

        for item, score, is_new, m in outliers:
            rows.append(_insider_row(
                _date(item), is_new, ticker, name, m.get("role") or "?", action,
                _num(m.get("value_usd")), score,
            ))

        if len(routine) >= DRIP_MIN_TRADES:
            dates = sorted(_date(e[0]) for e in routine)
            total = sum(v for v in (_num(e[3].get("value_usd")) for e in routine) if v is not None)
            rows.append(_insider_row(
                f"{dates[0]} → {dates[-1]}", any(e[2] for e in routine), ticker,
                name, routine[0][3].get("role") or "?", action,
                total if values else None, max(e[1] for e in routine), count=len(routine),
            ))
        else:
            for item, score, is_new, m in routine:
                rows.append(_insider_row(
                    _date(item), is_new, ticker, name, m.get("role") or "?", action,
                    _num(m.get("value_usd")), score,
                ))
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
    cutoff_win = now - timedelta(days=LEADER_WINDOW_DAYS)

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

    # Leaders come from a rolling window — an all-time list froze early
    # ⭐1.00 items (e.g. Clipped articles) at the top permanently.
    window_items = [
        (item, score) for item, score in tier_items
        if _parse_dt(item.get("ingested_at")) >= cutoff_win
    ]
    historical = [
        (item, score) for item, score in window_items[:top_n]
        if item.get("id") not in new_ids
    ]

    total = len(tier_items)
    shown = min(top_n, max(0, len(window_items) - len(new_items)))
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
        f"window_qualifying: {len(window_items)}\n"
        f"new_24h: {len(new_items)}\n"
        f"---"
    )

    header_callout = (
        f"> [!abstract] {emoji} {label} Signal — updated {now_utc}\n"
        f"> **Score** = `triage_score × confidence_weight × source_multiplier × regime_weight × sentiment_weight`\n"
        f"> {desc}\n"
        f"> Showing **{len(new_items)} new** + top **{shown}** from the last "
        f"{LEADER_WINDOW_DAYS} days ({len(window_items)} in window, {total} all-time)"
    )

    new_section    = _build_section("🆕 New — Last 24h", new_items, new_ids, outcomes)
    window_section = _build_section(
        f"📋 Leaders — Rolling {LEADER_WINDOW_DAYS} Days", historical, set(), outcomes
    )

    sections = [
        frontmatter,
        f"# {emoji} {label} Signal — Leaderboard",
        header_callout,
    ]
    if track_record_callout:
        sections.append(track_record_callout)
    sections += [
        "---",
        new_section,
        "---",
        window_section,
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
