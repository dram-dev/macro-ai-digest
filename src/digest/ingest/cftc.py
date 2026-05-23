"""CFTC Commitments of Traders (COT) ingestor.

Downloads the CFTC's weekly financial-futures COT report (short format, current
year), parses net speculative positioning, and generates items when positioning
changes are anomalous vs the trailing baseline. No auth required.

Only financial futures are tracked (f_year.txt) — equity index, Treasury, FX,
and CME crypto. Commodity futures are out of scope for this digest.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import datetime, timezone

import requests

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

# deacot{YYYY}.zip: legacy all-futures CSV, updated weekly. Financial futures
# are interleaved with commodities; we filter by contract name at parse time.
HEADERS = {"User-Agent": "macro-ai-digest/0.1 (research)"}
LOOKBACK_WEEKS = 12

# Maps a substring of the CFTC market name → (short label, topic_hint)
CURATED: dict[str, tuple[str, str]] = {
    "E-MINI S&P 500":              ("S&P 500 E-Mini",           "fed_markets"),
    "NASDAQ-100 STOCK INDEX (MINI)": ("Nasdaq 100 E-Mini",      "fed_markets"),
    "10-YEAR U.S. TREASURY NOTES": ("10Y T-Notes",              "fed_markets"),
    "U.S. TREASURY BONDS":         ("30Y T-Bonds",              "fed_markets"),
    "2-YEAR U.S. TREASURY NOTES":  ("2Y T-Notes",               "fed_markets"),
    "30-DAY FEDERAL FUNDS":        ("30-Day Fed Funds",          "fed_markets"),
    "3-MONTH SOFR":                ("3M SOFR",                   "fed_markets"),
    "U.S. DOLLAR INDEX":           ("USD Index",                 "fed_markets"),
    "EURO FX":                     ("EUR/USD",                   "fed_markets"),
    "JAPANESE YEN":                ("JPY",                       "china"),
    "BITCOIN":                     ("Bitcoin CME",               "ai_capex"),
}


def _match_contract(name: str) -> tuple[str, str] | None:
    name_upper = name.upper()
    for key, val in CURATED.items():
        if key in name_upper:
            return val
    return None


def _z_score(value: float, history: list[float]) -> float:
    if len(history) < 4:
        return 0.0
    n = len(history)
    mean = sum(history) / n
    stddev = (sum((x - mean) ** 2 for x in history) / max(n - 1, 1)) ** 0.5
    return (value - mean) / stddev if stddev > 0 else 0.0


class CFTCIngestor(IngestorBase):
    name = "cftc"

    def fetch(self) -> list[IngestedItem]:
        year = datetime.now(timezone.utc).year
        url = f"https://www.cftc.gov/files/dea/history/deacot{year}.zip"
        try:
            r = requests.get(url, headers=HEADERS, timeout=60)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                csv_name = next(
                    (n for n in zf.namelist() if n.endswith(".txt") or n.endswith(".csv")),
                    None,
                )
                if csv_name is None:
                    logger.warning("cftc: no .txt/.csv found in zip; files: %s", zf.namelist())
                    return []
                text = zf.read(csv_name).decode("latin-1", errors="replace")
        except Exception as exc:  # noqa: BLE001
            logger.warning("cftc: download failed: %s", exc)
            return []

        reader = csv.DictReader(io.StringIO(text))
        # Group rows by market name, keep in order
        by_market: dict[str, list[dict]] = {}
        for row in reader:
            name = row.get("Market and Exchange Names", "").strip().strip('"')
            if not name:
                continue
            match = _match_contract(name)
            if match is None:
                continue
            by_market.setdefault(name, []).append(row)

        items: list[IngestedItem] = []
        for market_name, rows in by_market.items():
            match = _match_contract(market_name)
            if not match:
                continue
            label, topic_hint = match

            # Rows are already in date order (oldest first in the year file)
            try:
                net_positions: list[float] = []
                dates: list[str] = []
                for row in rows:
                    nc_long = float((row.get("Noncommercial Positions-Long (All)") or "0").replace(",", "").strip())
                    nc_short = float((row.get("Noncommercial Positions-Short (All)") or "0").replace(",", "").strip())
                    net_positions.append(nc_long - nc_short)
                    dates.append(row.get("As of Date in Form YYYY-MM-DD", "").strip().strip('"'))

                if len(net_positions) < 3:
                    continue

                latest_net = net_positions[-1]
                prev_net = net_positions[-2]
                weekly_change = latest_net - prev_net

                history_changes = [
                    net_positions[i] - net_positions[i - 1]
                    for i in range(1, len(net_positions))
                ]
                recent_changes = history_changes[-LOOKBACK_WEEKS:]
                z = _z_score(weekly_change, recent_changes[:-1] if recent_changes else [])

                if abs(z) < settings.cftc_sigma_thresh:
                    continue

                date_str = dates[-1]
                try:
                    pub = datetime.strptime(date_str, "%Y-%m-%d")
                except ValueError:
                    pub = None

                direction = "extended long" if weekly_change > 0 else "reduced / flipped short"
                net_k = latest_net / 1000
                chg_k = weekly_change / 1000

                content = (
                    f"Speculative net position: {net_k:+.1f}k contracts ({weekly_change:+,.0f} wk change, z={z:+.2f}).\n"
                    f"Largest speculators {direction} by {abs(chg_k):.1f}k contracts.\n"
                    f"Prior week net: {prev_net / 1000:+.1f}k. "
                    f"Trailing {LOOKBACK_WEEKS}w baseline used for z-score."
                )

                items.append(IngestedItem(
                    source=self.name,
                    source_id=f"cot:{label.replace(' ', '_')}:{date_str}",
                    title=f"COT {label}: spec net {net_k:+.1f}k ({weekly_change:+,.0f} wk, z={z:+.1f})",
                    url="https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm",
                    content=content,
                    published_at=pub,
                    metadata={
                        "contract": label,
                        "net_position": latest_net,
                        "weekly_change": weekly_change,
                        "z_score": z,
                        "topic_hint": topic_hint,
                    },
                ))
            except Exception as exc:  # noqa: BLE001
                logger.warning("cftc: parse error on %s: %s", market_name, exc)

        return items
