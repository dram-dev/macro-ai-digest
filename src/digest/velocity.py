"""Narrative velocity — week-over-week cluster momentum (Feature 5).

Uses the existing cluster_id column to measure which narrative threads are
accelerating or fading week-over-week. Writes Obsidian note at:
  <vault>/80 Digest/Signal/Velocity.md

Run via: digest velocity
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)

_MIN_CLUSTER_TOTAL = 3


def _week_range(ref: date) -> tuple[str, str]:
    monday = ref - timedelta(days=ref.weekday())
    sunday = monday + timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def compute_velocity() -> list[dict]:
    """Return cluster velocity stats sorted by |velocity| descending."""
    today    = date.today()
    this_mon, this_sun = _week_range(today)
    last_mon, last_sun = _week_range(today - timedelta(weeks=1))

    this_week = db.cluster_counts_for_range(this_mon, this_sun)
    last_week = db.cluster_counts_for_range(last_mon, last_sun)

    results: list[dict] = []
    for cluster_id in set(this_week) | set(last_week):
        if not cluster_id:
            continue
        this_n = this_week.get(cluster_id, 0)
        last_n = last_week.get(cluster_id, 0)
        if this_n + last_n < _MIN_CLUSTER_TOTAL:
            continue

        is_new  = last_n == 0 and this_n > 0
        is_gone = this_n == 0 and last_n > 0
        if is_new:
            velocity = float("inf")
        elif is_gone:
            velocity = -1.0          # -100%: disappeared entirely
        else:
            velocity = (this_n - last_n) / last_n

        results.append({
            "cluster_id": cluster_id,
            "this_week":  this_n,
            "last_week":  last_n,
            "velocity":   velocity,
            "is_new":     is_new,
            "is_gone":    is_gone,
        })

    return sorted(results, key=lambda x: abs(x["velocity"]), reverse=True)


def _arrow(c: dict) -> str:
    if c["is_new"]:  return "🆕"
    if c["is_gone"]: return "💀"
    v = c["velocity"]
    if v > 0.5:  return "🔥"
    if v > 0.2:  return "↑"
    if v < -0.5: return "📉"
    if v < -0.2: return "↓"
    return "→"


def _wow_str(c: dict) -> str:
    if c["is_new"]:  return "NEW"
    if c["is_gone"]: return "-100%"
    return f"{c['velocity']:+.0%}"


def write_velocity_note() -> dict:
    """Compute velocity and write Obsidian note."""
    today    = date.today()
    clusters = compute_velocity()

    this_mon, _ = _week_range(today)
    last_mon, _ = _week_range(today - timedelta(weeks=1))

    vault      = Path(settings.obsidian_vault_path).expanduser()
    signal_dir = vault / settings.obsidian_digest_dir / "Signal"
    signal_dir.mkdir(parents=True, exist_ok=True)

    this_week_empty = all(c["this_week"] == 0 for c in clusters)
    stale_note = (
        "\n> [!warning] No items clustered yet for the current week — "
        "run `digest cluster` to refresh."
        if this_week_empty and clusters else ""
    )

    lines = [
        "---",
        f"updated: {today.isoformat()}",
        f"this_week_start: {this_mon}",
        f"last_week_start: {last_mon}",
        f"clusters_tracked: {len(clusters)}",
        "---",
        "",
        "# 📈 Narrative Velocity",
        f"> Week-over-week cluster momentum — as of {today.isoformat()}",
        f"> Cluster labels = top TF-IDF terms. NEW = first appearance this week.{stale_note}",
        "",
        "| Cluster | This Wk | Last Wk | WoW | Trend |",
        "|---------|---------|---------|-----|-------|",
    ]

    finite = [c for c in clusters if not c["is_new"]]
    new    = [c for c in clusters if c["is_new"]]

    for c in (finite + new)[:30]:
        lines.append(
            f"| `{c['cluster_id']}` | {c['this_week']} | {c['last_week']}"
            f" | {_wow_str(c)} | {_arrow(c)} |"
        )

    if not clusters:
        lines.append("| — | — | — | insufficient data | — |")

    new_clusters  = [c for c in clusters if c["is_new"]]
    gone_clusters = [c for c in clusters if c["is_gone"]]
    hot  = [c for c in clusters if not c["is_new"] and not c["is_gone"] and c["velocity"] > 0.2]
    cold = [c for c in clusters if not c["is_new"] and not c["is_gone"] and c["velocity"] < -0.2]

    def _cluster_examples(cluster_id: str, start: str, end: str, limit: int = 3) -> list[str]:
        rows = db.top_items_for_cluster(cluster_id, start, end, limit=limit)
        out = []
        for row in rows:
            date_str = (row["published_at"] or row["ingested_at"] or "")[:10]
            title    = (row["title"] or "?")[:80].replace("\n", " ")
            src      = row["source"] or "?"
            out.append(f"  - `{date_str}` [{title}]({row['url'] or ''}) *(via {src})*")
        return out

    lines += ["", "## 🆕 Emerging This Week", ""]
    if new_clusters:
        for c in new_clusters[:10]:
            lines.append(f"- **`{c['cluster_id']}`** — {c['this_week']} items (first appearance)")
            lines.extend(_cluster_examples(c["cluster_id"], this_mon, _week_range(today)[1]))
    else:
        lines.append("*No new clusters this week.*")

    lines += ["", "## 🔥 Accelerating", ""]
    if hot:
        for c in sorted(hot, key=lambda x: x["velocity"], reverse=True)[:10]:
            lines.append(f"- **`{c['cluster_id']}`** — {c['this_week']} this wk vs {c['last_week']} last ({_wow_str(c)} WoW)")
            lines.extend(_cluster_examples(c["cluster_id"], this_mon, _week_range(today)[1]))
    else:
        lines.append("*No strongly accelerating clusters this week.*")

    lines += ["", "## 📉 Fading", ""]
    if cold or gone_clusters:
        for c in sorted(cold, key=lambda x: x["velocity"])[:10]:
            lines.append(f"- **`{c['cluster_id']}`** — {c['this_week']} this wk vs {c['last_week']} last ({_wow_str(c)} WoW)")
            lines.extend(_cluster_examples(c["cluster_id"], this_mon, _week_range(today)[1]))
        for c in gone_clusters[:5]:
            lines.append(f"- **`{c['cluster_id']}`** — gone ({c['last_week']} items last week, 0 this week)")
            lines.extend(_cluster_examples(c["cluster_id"], last_mon, _week_range(today - timedelta(weeks=1))[1]))
    else:
        lines.append("*No strongly fading clusters this week.*")

    path = signal_dir / "Velocity.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("velocity: wrote %s (%d clusters)", path.name, len(clusters))
    return {"path": str(path), "clusters": len(clusters)}
