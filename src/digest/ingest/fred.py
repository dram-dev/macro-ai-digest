"""FRED ingestor — pulls configured series, flags prints that move >N sigma.

Only creates items for series where the latest observation is anomalous vs
a trailing 90-day weekly-delta baseline. No noise when nothing moves.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import yaml
from fredapi import Fred

from digest import db
from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

FRED_CONFIG = Path(__file__).resolve().parents[3] / "config" / "fred_series.yaml"
BASELINE_WINDOW_DAYS = 90
DEFAULT_SIGMA_THRESHOLD = 1.0


def _update_baseline(conn: sqlite3.Connection, series_id: str, deltas: list[float]) -> None:
    if len(deltas) < 4:
        return
    n = len(deltas)
    mean = sum(deltas) / n
    var = sum((d - mean) ** 2 for d in deltas) / max(n - 1, 1)
    stddev = var**0.5
    conn.execute(
        """
        INSERT INTO fred_baseline (series_id, mean_delta, stddev_delta, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(series_id) DO UPDATE SET
            mean_delta = excluded.mean_delta,
            stddev_delta = excluded.stddev_delta,
            updated_at = excluded.updated_at
        """,
        (series_id, mean, stddev),
    )


class FREDIngestor(IngestorBase):
    name = "fred"

    def __init__(self) -> None:
        if not settings.fred_api_key:
            raise RuntimeError("FRED_API_KEY not set")
        self.fred = Fred(api_key=settings.fred_api_key)
        self.config = yaml.safe_load(FRED_CONFIG.read_text())
        self.sigma_threshold = self.config.get("sigma_threshold", DEFAULT_SIGMA_THRESHOLD)

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        for series in self.config["series"]:
            sid = series["id"]
            label = series.get("label", sid)
            try:
                s = self.fred.get_series(sid)
                if s is None or s.empty or len(s) < 6:
                    logger.info("fred: %s has insufficient data", sid)
                    continue

                latest_date = s.index[-1]
                latest_value = float(s.iloc[-1])
                prev_value = float(s.iloc[-2])
                delta = latest_value - prev_value

                # Build trailing baseline of deltas
                recent = s.tail(20)
                recent_deltas = recent.diff().dropna().tolist()
                with db.get_conn() as conn:
                    _update_baseline(conn, sid, recent_deltas)
                    row = conn.execute(
                        "SELECT mean_delta, stddev_delta FROM fred_baseline WHERE series_id = ?",
                        (sid,),
                    ).fetchone()

                mean_d = row["mean_delta"] if row else 0.0
                std_d = row["stddev_delta"] if row else 0.0
                z = (delta - mean_d) / std_d if std_d and std_d > 0 else 0.0

                if abs(z) < self.sigma_threshold:
                    continue  # quiet

                direction = "up" if delta > 0 else "down"
                title = (
                    f"{label} moved {direction} {delta:+.3f} "
                    f"(z={z:+.2f}, latest={latest_value:.3f}, date={latest_date.date()})"
                )
                items.append(
                    IngestedItem(
                        source=self.name,
                        source_id=f"{sid}:{latest_date.date().isoformat()}",
                        title=title,
                        url=f"https://fred.stlouisfed.org/series/{sid}",
                        content=None,
                        published_at=latest_date.to_pydatetime()
                        if hasattr(latest_date, "to_pydatetime")
                        else datetime.combine(latest_date.date(), datetime.min.time()),
                        metadata={
                            "series_id": sid,
                            "label": label,
                            "latest_value": latest_value,
                            "prev_value": prev_value,
                            "delta": delta,
                            "z_score": z,
                            "baseline_mean": mean_d,
                            "baseline_stddev": std_d,
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("fred: failed on %s: %s", sid, exc)
        return items
