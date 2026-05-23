"""Signal backtest report — outcome aggregation by source × series/topic.

Aggregates signal_outcomes (7-day horizon) at two levels:
  1. Per-series (FRED/CBOE/CFTC): shows which specific series were
     confirmed or contradicted with z-score context.
  2. Per source × topic: overall confirmation rates.

Writes to: <vault>/80 Digest/Signal/Backtest.md
Run via: digest backtest
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)


def _fetch_source_topic_rows() -> list[dict]:
    sql = """
        SELECT
            i.source,
            COALESCE(i.topic, 'unknown') AS topic,
            COUNT(*) AS total,
            SUM(CASE WHEN so.outcome = 'confirmed'    THEN 1 ELSE 0 END) AS confirmed,
            SUM(CASE WHEN so.outcome = 'contradicted' THEN 1 ELSE 0 END) AS contradicted,
            SUM(CASE WHEN so.outcome = 'neutral'      THEN 1 ELSE 0 END) AS neutral,
            SUM(CASE WHEN so.outcome = 'pending'      THEN 1 ELSE 0 END) AS pending,
            AVG(ABS(so.original_z)) AS avg_z
        FROM signal_outcomes so
        JOIN items i ON i.id = so.item_id
        WHERE so.horizon_days = 7
        GROUP BY i.source, topic
        HAVING total >= 3
        ORDER BY confirmed DESC, total DESC
    """
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def _fetch_series_rows() -> list[dict]:
    """Per-series breakdown — extracts series label from item title."""
    sql = """
        SELECT
            i.source,
            i.title,
            so.outcome,
            so.original_z,
            so.followup_z,
            json_extract(i.metadata_json, '$.series_id')  AS series_id,
            json_extract(i.metadata_json, '$.label')      AS series_label,
            json_extract(i.metadata_json, '$.contract')   AS contract,
            json_extract(i.metadata_json, '$.symbol')     AS symbol,
            json_extract(i.metadata_json, '$.ticker')     AS ticker,
            date(i.ingested_at) AS signal_date
        FROM signal_outcomes so
        JOIN items i ON i.id = so.item_id
        WHERE so.horizon_days = 7
          AND i.source IN ('fred', 'cboe', 'cftc', 'yahoo')
        ORDER BY so.outcome, ABS(so.original_z) DESC
    """
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def _series_name(row: dict) -> str:
    return (
        row.get("series_label") or row.get("contract") or
        row.get("symbol") or row.get("ticker") or
        row.get("series_id") or "?"
    )


def _aggregate_by_series(rows: list[dict]) -> dict[str, dict]:
    by_series: dict[str, dict] = {}
    for r in rows:
        name = _series_name(r)
        if name not in by_series:
            by_series[name] = {
                "source": r["source"],
                "confirmed": 0, "contradicted": 0, "neutral": 0, "pending": 0,
                "z_scores": [], "examples": {"confirmed": [], "contradicted": []},
            }
        s = by_series[name]
        outcome = r["outcome"]
        s[outcome] = s.get(outcome, 0) + 1
        if r["original_z"] is not None:
            s["z_scores"].append(abs(float(r["original_z"])))
        if outcome in ("confirmed", "contradicted") and len(s["examples"][outcome]) < 2:
            s["examples"][outcome].append({
                "date": r["signal_date"] or "",
                "title": (r["title"] or "")[:80],
                "original_z": r["original_z"],
                "followup_z": r["followup_z"],
            })
    return by_series


def write_backtest_report() -> dict:
    """Write signal backtest markdown to Obsidian. Returns metadata."""
    today      = date.today()
    topic_rows = _fetch_source_topic_rows()
    series_rows = _fetch_series_rows()
    by_series  = _aggregate_by_series(series_rows)

    vault      = Path(settings.obsidian_vault_path).expanduser()
    signal_dir = vault / settings.obsidian_digest_dir / "Signal"
    signal_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        "---",
        f"updated: {today.isoformat()}",
        "horizon_days: 7",
        "---",
        "",
        "# 📊 Signal Backtest Report",
        f"> 7-day outcome tracking — {today.isoformat()}",
        "",
    ]

    if not topic_rows and not by_series:
        lines.append(
            "> [!note] No resolved outcomes yet.\n"
            "> Run `digest outcomes` after signals have aged 7 days to populate this report."
        )
        path = signal_dir / "Backtest.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return {"path": str(path), "rows": 0}

    # ── Overall summary ────────────────────────────────────────────────
    resolved_all  = sum(r["confirmed"] + r["contradicted"] + r["neutral"] for r in topic_rows)
    confirmed_all = sum(r["confirmed"] for r in topic_rows)
    rate_all      = confirmed_all / resolved_all if resolved_all else 0

    lines += [
        f"> [!{'success' if rate_all >= 0.5 else 'warning'}] **{rate_all:.0%}** overall confirmation rate "
        f"({confirmed_all} confirmed / {resolved_all} resolved)",
        "",
    ]

    # ── Per-series breakdown (the useful section) ──────────────────────
    if by_series:
        lines += [
            "## Series-Level Breakdown",
            "",
            "| Series | Src | ✅ | ❌ | ⚖️ | ⏳ | Rate | Avg&#124;z&#124; |",
            "|--------|-----|----|----|----|----|------|---------|",
        ]

        def _sort_key(item: tuple[str, dict]) -> tuple:
            name, s = item
            resolved = s["confirmed"] + s["contradicted"] + s["neutral"]
            rate = s["confirmed"] / resolved if resolved else -1
            return (-resolved, -rate)

        for name, s in sorted(by_series.items(), key=_sort_key):
            resolved = s["confirmed"] + s["contradicted"] + s["neutral"]
            rate_str = f"{s['confirmed'] / resolved:.0%}" if resolved >= 2 else "—"
            avg_z    = f"{sum(s['z_scores']) / len(s['z_scores']):.2f}" if s["z_scores"] else "—"
            lines.append(
                f"| **{name}** | {s['source']} | {s['confirmed']} | {s['contradicted']}"
                f" | {s['neutral']} | {s['pending']} | {rate_str} | {avg_z} |"
            )

        lines.append("")

        # Show representative examples for top-confirmed and most-contradicted
        strong = sorted(
            [(n, s) for n, s in by_series.items()
             if s["confirmed"] + s["contradicted"] + s["neutral"] >= 2],
            key=lambda x: x[1]["confirmed"] / max(x[1]["confirmed"] + x[1]["contradicted"] + x[1]["neutral"], 1),
            reverse=True,
        )
        weak = sorted(
            [(n, s) for n, s in by_series.items()
             if s["confirmed"] + s["contradicted"] + s["neutral"] >= 2],
            key=lambda x: x[1]["contradicted"] / max(x[1]["confirmed"] + x[1]["contradicted"] + x[1]["neutral"], 1),
            reverse=True,
        )

        if strong:
            lines += ["## ✅ Most Reliable Series", ""]
            for name, s in strong[:4]:
                resolved = s["confirmed"] + s["contradicted"] + s["neutral"]
                rate = s["confirmed"] / resolved
                lines.append(
                    f"### {name} ({rate:.0%} confirmed, {resolved} resolved)"
                )
                for ex in s["examples"].get("confirmed", [])[:2]:
                    oz = f"{float(ex['original_z']):+.2f}σ" if ex["original_z"] is not None else "?"
                    fz = f"{float(ex['followup_z']):+.2f}σ" if ex["followup_z"] is not None else "?"
                    lines.append(f"- ✅ `{ex['date']}` z={oz} → {fz} | {ex['title']}")
                lines.append("")

        if weak and weak[0][0] not in {n for n, _ in strong[:2]}:
            lines += ["## ❌ Least Reliable Series", ""]
            for name, s in weak[:4]:
                resolved = s["confirmed"] + s["contradicted"] + s["neutral"]
                contra_rate = s["contradicted"] / resolved
                if contra_rate < 0.4:
                    continue
                lines.append(
                    f"### {name} ({contra_rate:.0%} contradicted, {resolved} resolved)"
                )
                for ex in s["examples"].get("contradicted", [])[:2]:
                    oz = f"{float(ex['original_z']):+.2f}σ" if ex["original_z"] is not None else "?"
                    fz = f"{float(ex['followup_z']):+.2f}σ" if ex["followup_z"] is not None else "?"
                    lines.append(f"- ❌ `{ex['date']}` z={oz} → {fz} | {ex['title']}")
                lines.append("")

    # ── Source × topic summary (compact) ──────────────────────────────
    if topic_rows:
        lines += [
            "## Source × Topic Summary",
            "",
            "| Source | Topic | Total | ✅ | ❌ | ⚖️ | ⏳ | Rate |",
            "|--------|-------|-------|----|----|----|----|------|",
        ]
        for r in topic_rows:
            resolved = r["confirmed"] + r["contradicted"] + r["neutral"]
            acc      = f"{r['confirmed'] / resolved:.0%}" if resolved >= 3 else "—"
            lines.append(
                f"| {r['source']} | {r['topic']} | {r['total']}"
                f" | {r['confirmed']} | {r['contradicted']} | {r['neutral']} | {r['pending']}"
                f" | {acc} |"
            )

    path = signal_dir / "Backtest.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("backtest: wrote %s (%d series, %d topic-rows)", path.name, len(by_series), len(topic_rows))
    return {"path": str(path), "rows": len(topic_rows), "series": len(by_series)}
