"""Chart generation for the daily Obsidian note.

Two rendering modes are produced side-by-side in ## Market Snapshot so the
user can compare plugin vs PNG rendering across devices:

  1. Obsidian Charts code blocks  — rendered by the community plugin (Chart.js)
  2. Composite matplotlib PNG     — ![[YYYY-MM-DD.png]] works everywhere

Both are generated from the same DB query so the charts are always in sync.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

from digest import db

logger = logging.getLogger(__name__)

# ── Palette (rgba strings; work in Chart.js and map to matplotlib below) ────
_RED   = "rgba(231,76,60,0.75)"
_BLUE  = "rgba(52,152,219,0.75)"
_GREEN = "rgba(46,204,113,0.75)"
_GREY  = "rgba(149,165,166,0.75)"

_RED_B   = "rgba(231,76,60,1)"
_BLUE_B  = "rgba(52,152,219,1)"
_GREEN_B = "rgba(46,204,113,1)"


def _sign_colors(values: list[float], pos: str, neg: str) -> list[str]:
    return [pos if v >= 0 else neg for v in values]


# ── Data queries ─────────────────────────────────────────────────────────────

def _query_signals(date_iso: str) -> list[dict]:
    """FRED / CBOE items with a z_score for the given date, sorted by |z|."""
    sql = """
        SELECT source, metadata_json
        FROM items
        WHERE date(ingested_at) = ?
          AND source IN ('fred', 'cboe', 'cftc')
          AND metadata_json IS NOT NULL
    """
    with db.get_conn() as conn:
        rows = conn.execute(sql, (date_iso,)).fetchall()
    out = []
    for row in rows:
        try:
            m = json.loads(row["metadata_json"] or "{}")
            z = m.get("z_score")
            if z is None:
                continue
            label = (
                m.get("series_id") or m.get("contract") or m.get("series") or row["source"]
            )
            out.append({"label": str(label)[:18], "z": round(z, 2)})
        except Exception:
            continue
    return sorted(out, key=lambda x: abs(x["z"]), reverse=True)[:12]


def _query_yahoo(date_iso: str) -> list[dict]:
    """Yahoo Finance items with pct_change_1d for the given date."""
    sql = """
        SELECT metadata_json
        FROM items
        WHERE date(ingested_at) = ?
          AND source = 'yahoo'
          AND metadata_json IS NOT NULL
    """
    with db.get_conn() as conn:
        rows = conn.execute(sql, (date_iso,)).fetchall()
    out = []
    for row in rows:
        try:
            m = json.loads(row["metadata_json"] or "{}")
            pct = m.get("pct_change") or m.get("pct_change_1d")
            ticker = m.get("ticker")
            if pct is None or not ticker:
                continue
            out.append({"label": ticker, "pct": round(pct, 2), "rsi": m.get("rsi14")})
        except Exception:
            continue
    return sorted(out, key=lambda x: abs(x["pct"]), reverse=True)[:10]


def _query_cftc(date_iso: str) -> list[dict]:
    """CFTC items with net_position for the given date."""
    sql = """
        SELECT metadata_json
        FROM items
        WHERE date(ingested_at) = ?
          AND source = 'cftc'
          AND metadata_json IS NOT NULL
    """
    with db.get_conn() as conn:
        rows = conn.execute(sql, (date_iso,)).fetchall()
    out = []
    for row in rows:
        try:
            m = json.loads(row["metadata_json"] or "{}")
            net = m.get("net_position")
            contract = m.get("contract")
            if net is None or not contract:
                continue
            out.append({"label": str(contract)[:15], "net_k": round(net / 1000, 1)})
        except Exception:
            continue
    return sorted(out, key=lambda x: abs(x["net_k"]), reverse=True)[:10]


# ── Obsidian Charts blocks ────────────────────────────────────────────────────

def _chart_block(
    chart_type: str,
    labels: list,
    series: list[dict],
    begin_at_zero: bool = False,
    width: str = "100%",
) -> str:
    """Render a single Obsidian Charts plugin code block.

    The plugin schema uses 'series' (not Chart.js 'datasets') and 'title'
    (not 'label') — the error 'Missing type, labels or series' means the
    plugin rejected a Chart.js-native datasets block.
    """
    cfg: dict = {
        "type": chart_type,
        "labels": labels,
        "series": series,
        "beginAtZero": begin_at_zero,
        "width": width,
    }
    inner = yaml.dump(cfg, allow_unicode=True, sort_keys=False, width=120)
    return f"```chart\n{inner}```"


def build_obsidian_charts(date_iso: str) -> str:
    """Return markdown containing all Obsidian Charts blocks for the day.

    Returns empty string if there is no quantitative data for the date.
    The plugin auto-assigns colors per series; per-bar sign coloring is
    handled by the PNG panel instead.
    """
    parts: list[str] = []

    signals = _query_signals(date_iso)
    if signals:
        labels = [s["label"] for s in signals]
        zs = [s["z"] for s in signals]
        # Split into two series so the plugin can color them differently
        pos_data = [v if v >= 0 else 0 for v in zs]
        neg_data = [v if v < 0 else 0 for v in zs]
        series = [{"title": "Above baseline", "data": pos_data}]
        if any(v != 0 for v in neg_data):
            series.append({"title": "Below baseline", "data": neg_data})
        parts.append("**Signal Strength** — z-score vs trailing baseline")
        parts.append(_chart_block("bar", labels, series))

    yahoo = _query_yahoo(date_iso)
    if yahoo:
        labels = [y["label"] for y in yahoo]
        pcts = [y["pct"] for y in yahoo]
        pos_data = [v if v >= 0 else 0 for v in pcts]
        neg_data = [v if v < 0 else 0 for v in pcts]
        series = [{"title": "Gained", "data": pos_data}]
        if any(v != 0 for v in neg_data):
            series.append({"title": "Declined", "data": neg_data})
        parts.append("**Watchlist Daily Moves** — % change")
        parts.append(_chart_block("bar", labels, series))

    cftc = _query_cftc(date_iso)
    if cftc:
        labels = [c["label"] for c in cftc]
        nets = [c["net_k"] for c in cftc]
        pos_data = [v if v >= 0 else 0 for v in nets]
        neg_data = [v if v < 0 else 0 for v in nets]
        series = [{"title": "Net long", "data": pos_data}]
        if any(v != 0 for v in neg_data):
            series.append({"title": "Net short", "data": neg_data})
        parts.append("**CFTC Positioning** — k contracts net long/short")
        parts.append(_chart_block("bar", labels, series))

    return "\n\n".join(parts)


# ── Matplotlib PNG ────────────────────────────────────────────────────────────

def render_png(date_iso: str, assets_dir: Path) -> str:
    """Generate a composite bar-chart PNG and save it to assets_dir.

    Returns the Obsidian wikilink embed string (![[filename.png]]),
    or empty string if matplotlib is unavailable or there is no data.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("charts: matplotlib not installed — skipping PNG")
        return ""

    signals = _query_signals(date_iso)
    yahoo   = _query_yahoo(date_iso)
    cftc    = _query_cftc(date_iso)
    panels  = [(p, t) for p, t in [
        (signals, "Signal Strength\n(z-score vs baseline)"),
        (yahoo,   "Watchlist Moves\n(% daily change)"),
        (cftc,    "CFTC Positioning\n(k contracts, net)"),
    ] if p]

    if not panels:
        return ""

    n = len(panels)
    fig, axes = plt.subplots(1, n, figsize=(5.5 * n, max(3.5, 0.4 * max(len(p) for p, _ in panels) + 1.2)))
    if n == 1:
        axes = [axes]

    plt.rcParams.update({
        "font.family": "sans-serif",
        "font.size": 8,
        "axes.spines.top": False,
        "axes.spines.right": False,
    })

    def _barh(ax: plt.Axes, data: list[dict], value_key: str, title: str, unit: str):
        labels = [d["label"] for d in data]
        values = [d[value_key] for d in data]
        pos_c = "#2ecc71" if value_key == "pct" or value_key == "net_k" else "#e74c3c"
        neg_c = "#e74c3c" if value_key == "pct" or value_key == "net_k" else "#3498db"
        colors = [pos_c if v >= 0 else neg_c for v in values]
        y = range(len(labels))
        bars = ax.barh(list(y), values, color=colors, edgecolor="white", linewidth=0.3, height=0.6)
        ax.set_yticks(list(y))
        ax.set_yticklabels(labels, fontsize=7.5)
        ax.set_title(title, fontsize=9, fontweight="bold", pad=6)
        ax.set_xlabel(unit, fontsize=7, color="#666")
        ax.axvline(0, color="#999", linewidth=0.7, zorder=0)
        ax.spines["left"].set_color("#ddd")
        ax.spines["bottom"].set_color("#ddd")
        ax.tick_params(axis="x", labelsize=7, colors="#555")
        ax.tick_params(axis="y", colors="#333")
        # Value labels
        for bar, val in zip(bars, values):
            offset = max(abs(val) * 0.03, 0.05)
            ha = "left" if val >= 0 else "right"
            x = val + (offset if val >= 0 else -offset)
            ax.text(x, bar.get_y() + bar.get_height() / 2,
                    f"{val:+.1f}", va="center", ha=ha, fontsize=6.5, color="#333")

    _key_map = {"z": "z", "pct": "pct", "net_k": "net_k"}
    _unit_map = {
        "Signal Strength\n(z-score vs baseline)": "z-score",
        "Watchlist Moves\n(% daily change)":      "% change",
        "CFTC Positioning\n(k contracts, net)":   "k contracts",
    }
    _val_map = {
        "Signal Strength\n(z-score vs baseline)": "z",
        "Watchlist Moves\n(% daily change)":      "pct",
        "CFTC Positioning\n(k contracts, net)":   "net_k",
    }

    for ax, (data, title) in zip(axes, panels):
        _barh(ax, data, _val_map[title], title, _unit_map[title])

    fig.suptitle(f"Market Snapshot — {date_iso}", fontsize=11, fontweight="bold", y=1.01)
    plt.tight_layout()

    assets_dir.mkdir(parents=True, exist_ok=True)
    out_path = assets_dir / f"{date_iso}.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)

    logger.info("charts: PNG saved → %s", out_path)
    return f"![[{date_iso}.png]]"
