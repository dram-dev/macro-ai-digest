"""SEC Fails-to-Deliver (FTD) ingestor.

Downloads the SEC's bimonthly FTD CSV ZIP files, filters for tickers in the
configured watchlist, and generates items when FTD quantity or value crosses a
significant threshold. FTD data lags by ~2 weeks (T+17 business days).

Source: https://www.sec.gov/data/foiadocs/fails.htm
"""
from __future__ import annotations

import io
import logging
import zipfile
from datetime import datetime, timezone

import requests

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "macro-ai-digest/0.1 (research)"}
FTD_BASE = "https://www.sec.gov/files/data/fails-deliver-data/cnsfails{year}{month:02d}{half}.zip"

# Generate items only when FTD quantity or value exceeds these thresholds
MIN_FTD_SHARES = 50_000
MIN_FTD_VALUE = 10_000_000   # USD

SEMIS = {"NVDA", "AMD", "TSM", "INTC", "AVGO", "QCOM", "ASML", "MRVL"}


def _topic_for(ticker: str) -> str:
    return "ai_semis" if ticker.upper() in SEMIS else "ai_capex"


def _most_recent_url() -> tuple[str, str]:
    """Return (url, period_label) for the most recently published FTD file."""
    now = datetime.now(timezone.utc)
    year = now.year
    month = now.month
    day = now.day

    # SEC publishes: first-half data (1-15) ~25th of same month
    #                second-half data (16-EOM) ~10th of following month
    # We try most recent half first, fall back to prior if it 404s.
    if day >= 15:
        half, period = "b", f"{year}-{month:02d} second half"
    else:
        half, period = "a", f"{year}-{month:02d} first half"

    url = FTD_BASE.format(year=year, month=month, half=half)
    return url, period


def _try_urls() -> tuple[bytes | None, str]:
    """Try current and fallback FTD URLs, return (raw_bytes, period_label)."""
    now = datetime.now(timezone.utc)
    year, month, day = now.year, now.month, now.day

    candidates = []
    # Try this month's second half, this month's first half, prior month's second half
    candidates.append((year, month, "b", f"{year}-{month:02d} 2nd half"))
    candidates.append((year, month, "a", f"{year}-{month:02d} 1st half"))
    pm = month - 1 or 12
    py = year if month > 1 else year - 1
    candidates.append((py, pm, "b", f"{py}-{pm:02d} 2nd half"))

    for yr, mo, half, label in candidates:
        url = FTD_BASE.format(year=yr, month=mo, half=half)
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.content, label
        except Exception:  # noqa: BLE001
            continue
    return None, ""


class FTDIngestor(IngestorBase):
    name = "ftd"

    def __init__(self) -> None:
        self.watchlist = {
            t.strip().upper()
            for t in settings.yahoo_tickers.split(",")
            if t.strip()
        }
        if not self.watchlist:
            # Fallback watchlist if YAHOO_TICKERS not configured
            self.watchlist = {"NVDA", "AMD", "TSM", "MSFT", "GOOGL", "AMZN", "META", "INTC"}

    def fetch(self) -> list[IngestedItem]:
        raw, period = _try_urls()
        if not raw:
            logger.warning("ftd: could not download any FTD file")
            return []

        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                csv_name = next(n for n in zf.namelist() if n.endswith(".csv") or n.endswith(".txt"))
                csv_bytes = zf.read(csv_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ftd: ZIP extract failed: %s", exc)
            return []

        # Parse pipe-delimited CSV
        # Columns: SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE
        try:
            text = csv_bytes.decode("utf-8", errors="replace")
        except Exception:
            text = csv_bytes.decode("latin-1", errors="replace")

        rows = text.strip().splitlines()
        if not rows:
            return []

        # Skip header
        data_rows = rows[1:] if "|" in rows[0] and "SYMBOL" in rows[0].upper() else rows

        by_ticker: dict[str, dict] = {}
        for line in data_rows:
            parts = line.split("|")
            if len(parts) < 5:
                continue
            settle_date = parts[0].strip()
            symbol = parts[2].strip().upper()
            if symbol not in self.watchlist:
                continue
            try:
                qty = float(parts[3].strip().replace(",", ""))
                price_str = parts[5].strip().replace(",", "") if len(parts) > 5 else "0"
                price = float(price_str) if price_str else 0.0
            except ValueError:
                continue
            value = qty * price
            # Keep the row with the highest FTD quantity per ticker
            if symbol not in by_ticker or qty > by_ticker[symbol]["qty"]:
                by_ticker[symbol] = {
                    "settle_date": settle_date,
                    "qty": qty,
                    "price": price,
                    "value": value,
                    "desc": parts[4].strip() if len(parts) > 4 else symbol,
                }

        items: list[IngestedItem] = []
        for ticker, rec in by_ticker.items():
            if rec["qty"] < MIN_FTD_SHARES and rec["value"] < MIN_FTD_VALUE:
                continue

            settle = rec["settle_date"]
            try:
                pub = datetime.strptime(settle, "%Y%m%d")
            except ValueError:
                pub = None

            qty_k = rec["qty"] / 1000
            val_m = rec["value"] / 1_000_000
            content = (
                f"Fails-to-deliver for {ticker} ({period}): "
                f"{qty_k:.0f}k shares (${val_m:.1f}M at ${rec['price']:.2f}).\n"
                f"Settlement date: {settle}.\n"
                f"Elevated FTD can precede short-squeeze dynamics or signal settlement stress."
            )
            items.append(IngestedItem(
                source=self.name,
                source_id=f"ftd:{ticker}:{settle}",
                title=f"{ticker} FTD: {qty_k:.0f}k shares (${val_m:.1f}M) — {period}",
                url="https://www.sec.gov/data/foiadocs/fails.htm",
                content=content,
                published_at=pub,
                metadata={
                    "ticker": ticker,
                    "qty_shares": rec["qty"],
                    "value_usd": rec["value"],
                    "price": rec["price"],
                    "period": period,
                    "topic_hint": _topic_for(ticker),
                },
            ))

        logger.info("ftd: %d tickers matched, %d above threshold (%s)", len(by_ticker), len(items), period)
        return items
