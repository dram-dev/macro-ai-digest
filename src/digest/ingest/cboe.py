"""CBOE ingestor — VIX daily history and put/call ratios.

Downloads CBOE's public CSV files. Applies the same z-score anomaly filter as
the FRED ingestor: only generates items when a reading is statistically
significant vs its recent baseline. Quiet days produce no items.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime

import requests

from digest import db
from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "macro-ai-digest/0.1 (research)"}
LOOKBACK = 60       # rows used to build baseline

CBOE_SOURCES = {
    "vix": "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
    # P/C ratio endpoint blocked by Cloudflare as of 2026-05; VIX-only for now.
}

TOPIC_HINT = "fed_markets"


def _fetch_csv(url: str) -> list[list[str]] | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        # Skip header, parse remaining
        reader = [line.split(",") for line in lines[1:] if line.strip()]
        return reader
    except Exception as exc:  # noqa: BLE001
        logger.warning("cboe: failed to fetch %s: %s", url, exc)
        return None


def _z_score(value: float, history: list[float]) -> float:
    if len(history) < 5:
        return 0.0
    n = len(history)
    mean = sum(history) / n
    stddev = (sum((x - mean) ** 2 for x in history) / max(n - 1, 1)) ** 0.5
    return (value - mean) / stddev if stddev > 0 else 0.0


class CBOEIngestor(IngestorBase):
    name = "cboe"

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []

        # ── VIX ──────────────────────────────────────────────────────
        vix_rows = _fetch_csv(CBOE_SOURCES["vix"])
        if vix_rows:
            try:
                # columns: DATE, OPEN, HIGH, LOW, CLOSE
                closes = []
                dates = []
                for row in vix_rows:
                    if len(row) < 5:
                        continue
                    try:
                        closes.append(float(row[4]))
                        dates.append(row[0].strip())
                    except ValueError:
                        continue

                if len(closes) >= LOOKBACK + 2:
                    recent_closes = closes[-(LOOKBACK + 1):-1]
                    recent_changes = [closes[i] - closes[i-1] for i in range(-LOOKBACK, 0)]
                    latest_close = closes[-1]
                    prev_close = closes[-2]
                    daily_change = latest_close - prev_close
                    z = _z_score(daily_change, recent_changes)

                    if abs(z) >= settings.cboe_sigma_thresh:
                        direction = "spike" if daily_change > 0 else "drop"
                        date_str = dates[-1]
                        try:
                            pub = datetime.strptime(date_str, "%m/%d/%Y")
                        except ValueError:
                            pub = None

                        pct = daily_change / prev_close * 100
                        level = (
                            "extreme fear" if latest_close > 30
                            else "elevated" if latest_close > 20
                            else "complacent" if latest_close < 15
                            else "normal"
                        )
                        content = (
                            f"VIX {direction}: {daily_change:+.2f} pts to {latest_close:.2f} "
                            f"({pct:+.1f}%, z={z:+.2f}).\n"
                            f"Regime: {level} ({latest_close:.1f}).\n"
                            f"Prior close: {prev_close:.2f}."
                        )
                        items.append(IngestedItem(
                            source=self.name,
                            source_id=f"vix:{date_str.replace('/', '-')}",
                            title=f"VIX {direction} {daily_change:+.2f} to {latest_close:.2f} (z={z:+.1f})",
                            url="https://www.cboe.com/tradable_products/vix/",
                            content=content,
                            published_at=pub,
                            metadata={
                                "series": "VIX",
                                "close": latest_close,
                                "change": daily_change,
                                "z_score": z,
                                "topic_hint": TOPIC_HINT,
                            },
                        ))
            except Exception as exc:  # noqa: BLE001
                logger.warning("cboe: VIX parse failed: %s", exc)

        return items
