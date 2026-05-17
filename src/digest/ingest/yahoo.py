"""Yahoo Finance ingestor — daily price + technical indicators for a watchlist.

Uses yfinance (wrapper over Yahoo Finance's undocumented API). Only generates
items when a ticker shows a statistically significant price move OR when RSI
crosses extreme thresholds — quiet days produce no items.

Configure the watchlist via YAHOO_TICKERS in .env (comma-separated).
Default covers AI capex hyperscalers + key semis.
"""
from __future__ import annotations

import logging
from datetime import datetime

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase
from digest.indicators import latest_snapshot

logger = logging.getLogger(__name__)

# Tickers whose primary signal is AI semis vs AI capex spend
SEMIS = {"NVDA", "AMD", "TSM", "ASML", "INTC", "QCOM", "AVGO", "MRVL"}

# Thresholds for item generation
MOVE_THRESH_PCT = 2.5   # daily % move to always surface
RSI_OVERBOUGHT = 75.0
RSI_OVERSOLD = 28.0


def _topic_for(ticker: str) -> str:
    return "ai_semis" if ticker.upper() in SEMIS else "ai_capex"


def _format_content(ticker: str, snap: dict) -> str:
    lines = [f"Price: ${snap['price']:.2f}"]
    if snap.get("pct_change_1d") is not None:
        lines[0] += f" ({snap['pct_change_1d']:+.1f}% vs prev close)"
    if snap.get("rsi14") is not None:
        rsi_val = snap["rsi14"]
        flag = " ⚠ overbought" if rsi_val > RSI_OVERBOUGHT else " ⚠ oversold" if rsi_val < RSI_OVERSOLD else ""
        lines.append(f"RSI(14): {rsi_val:.1f}{flag}")
    if snap.get("pct_vs_sma50") is not None:
        dist = snap["pct_vs_sma50"]
        rel = "above" if dist > 0 else "below"
        lines.append(f"vs 50-day SMA: {dist:+.1f}% {rel} (SMA50 ${snap['sma50']:.2f})")
    if snap.get("macd") is not None and snap.get("macd_signal") is not None:
        cross = "bullish" if snap["macd"] > snap["macd_signal"] else "bearish"
        lines.append(f"MACD: {snap['macd']:.3f} vs signal {snap['macd_signal']:.3f} ({cross})")
    return "\n".join(lines)


class YahooIngestor(IngestorBase):
    name = "yahoo"

    def __init__(self) -> None:
        try:
            import yfinance as yf  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("yfinance not installed — run: uv add yfinance") from exc

    def fetch(self) -> list[IngestedItem]:
        import yfinance as yf

        tickers = [t.strip().upper() for t in settings.yahoo_tickers.split(",") if t.strip()]
        if not tickers:
            logger.info("yahoo: YAHOO_TICKERS is empty, skipping")
            return []

        items: list[IngestedItem] = []
        for ticker in tickers:
            try:
                hist = yf.Ticker(ticker).history(period="90d", auto_adjust=True)
                if hist is None or hist.empty or len(hist) < 30:
                    logger.info("yahoo: %s insufficient history (%d rows)", ticker, len(hist) if hist is not None else 0)
                    continue

                prices = [float(v) for v in hist["Close"].tolist()]
                dates = hist.index.tolist()
                snap = latest_snapshot(prices)
                if not snap:
                    continue

                pct = snap.get("pct_change_1d", 0.0) or 0.0
                rsi_val = snap.get("rsi14")

                # Only surface significant moves or RSI extremes
                rsi_extreme = rsi_val is not None and (rsi_val > RSI_OVERBOUGHT or rsi_val < RSI_OVERSOLD)
                if abs(pct) < MOVE_THRESH_PCT and not rsi_extreme:
                    continue

                latest_date = dates[-1]
                pub = latest_date.to_pydatetime() if hasattr(latest_date, "to_pydatetime") else datetime.combine(latest_date.date(), datetime.min.time())
                date_str = latest_date.date().isoformat() if hasattr(latest_date, "date") else str(latest_date)

                reason = f"{pct:+.1f}% move" if abs(pct) >= MOVE_THRESH_PCT else f"RSI {rsi_val:.0f}"
                title = f"{ticker}: {reason} to ${snap['price']:.2f}"

                items.append(IngestedItem(
                    source=self.name,
                    source_id=f"{ticker}:{date_str}",
                    title=title,
                    url=f"https://finance.yahoo.com/quote/{ticker}",
                    content=_format_content(ticker, snap),
                    published_at=pub,
                    metadata={
                        "ticker": ticker,
                        "pct_change": pct,
                        "rsi14": rsi_val,
                        "price": snap["price"],
                        "topic_hint": _topic_for(ticker),
                    },
                ))
                logger.info("yahoo: %s %s pct=%.1f rsi=%s", ticker, date_str, pct, f"{rsi_val:.0f}" if rsi_val else "n/a")
            except Exception as exc:  # noqa: BLE001
                logger.warning("yahoo: failed on %s: %s", ticker, exc)

        return items
