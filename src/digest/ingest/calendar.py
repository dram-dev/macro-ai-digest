"""Forward event calendar ingestor (Feature 2).

Sources:
  - FOMC meeting dates (hardcoded 2025–2026, federalreserve.gov)
  - Key macro data release approximations (rolling 90-day window)
  - Earnings dates via yfinance (configured tickers, best-effort)

Stores events in the upcoming_events table (upsert, idempotent).
Run via: digest calendar
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)

# ── FOMC dates 2025–2026 (source: federalreserve.gov) ─────────────────

_FOMC_DATES = [
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]

# ── Approximate monthly macro release schedule ─────────────────────────
# day_hint = typical business day of release; actual dates shift each month.

_MACRO_RELEASES = [
    {"title": "Nonfarm Payrolls",           "type": "fred_release", "day_hint": 5},
    {"title": "ISM Manufacturing PMI",      "type": "fred_release", "day_hint": 1},
    {"title": "ISM Services PMI",           "type": "fred_release", "day_hint": 3},
    {"title": "Initial Jobless Claims",     "type": "fred_release", "day_hint": 4},
    {"title": "CPI Report",                 "type": "fred_release", "day_hint": 10},
    {"title": "PPI Report",                 "type": "fred_release", "day_hint": 11},
    {"title": "Consumer Sentiment (UMich)", "type": "fred_release", "day_hint": 14},
    {"title": "Retail Sales",               "type": "fred_release", "day_hint": 15},
    {"title": "Industrial Production",      "type": "fred_release", "day_hint": 16},
    {"title": "JOLTS Job Openings",         "type": "fred_release", "day_hint": 8},
    {"title": "PCE / Core PCE",             "type": "fred_release", "day_hint": 28},
    {"title": "GDP Advance Estimate",       "type": "fred_release", "day_hint": 25},
]


def _fomc_events() -> list[dict]:
    today = date.today()
    events = []
    for ds in _FOMC_DATES:
        d = date.fromisoformat(ds)
        if d >= today - timedelta(days=1):
            events.append({
                "event_type":    "fomc",
                "event_date":    ds,
                "title":         f"FOMC Meeting — {d.strftime('%b %Y')}",
                "symbol":        None,
                "metadata_json": json.dumps({"source": "federalreserve.gov"}),
            })
    return events


def _macro_release_events() -> list[dict]:
    today  = date.today()
    cutoff = today + timedelta(days=90)
    events = []
    for offset in range(4):
        month = today.month + offset
        year  = today.year + (month - 1) // 12
        month = ((month - 1) % 12) + 1
        for rel in _MACRO_RELEASES:
            day = min(rel["day_hint"], 28)
            try:
                d = date(year, month, day)
            except ValueError:
                continue
            if today <= d <= cutoff:
                events.append({
                    "event_type":    rel["type"],
                    "event_date":    d.isoformat(),
                    "title":         rel["title"],
                    "symbol":        None,
                    "metadata_json": json.dumps({"approximate": True, "day_hint": day}),
                })
    return events


def _earnings_events() -> list[dict]:
    try:
        import yfinance as yf
    except ImportError:
        return []

    raw = getattr(settings, "yahoo_tickers", None) or ""
    tickers = [t.strip() for t in str(raw).split(",") if t.strip()] if raw else []
    if not tickers:
        return []

    today  = date.today()
    cutoff = today + timedelta(days=60)
    events = []

    for ticker in tickers[:20]:
        try:
            cal = yf.Ticker(ticker).calendar
            if cal is None:
                continue
            # yfinance returns dict or DataFrame depending on version
            if isinstance(cal, dict):
                ed_raw = cal.get("Earnings Date")
                # yfinance returns a list of dates
                if isinstance(ed_raw, list):
                    ed_raw = ed_raw[0] if ed_raw else None
            elif hasattr(cal, "loc"):
                ed_raw = cal.loc["Earnings Date"].iloc[0] if "Earnings Date" in cal.index else None
            else:
                continue
            if ed_raw is None:
                continue
            ed = ed_raw if isinstance(ed_raw, date) else (
                ed_raw.date() if hasattr(ed_raw, "date") else date.fromisoformat(str(ed_raw)[:10])
            )
            if today <= ed <= cutoff:
                events.append({
                    "event_type":    "earnings",
                    "event_date":    ed.isoformat(),
                    "title":         f"{ticker} Earnings",
                    "symbol":        ticker,
                    "metadata_json": json.dumps({"ticker": ticker}),
                })
        except Exception as exc:
            logger.debug("earnings fetch skipped for %s: %s", ticker, exc)

    return events


def run_calendar() -> dict[str, int]:
    """Refresh upcoming_events table. Returns event counts by type."""
    pruned = db.prune_past_events(days_grace=1)
    if pruned:
        logger.info("calendar: pruned %d expired events", pruned)

    events: list[dict] = []
    events.extend(_fomc_events())
    events.extend(_macro_release_events())
    events.extend(_earnings_events())
    db.upsert_events(events)
    counts: dict[str, int] = {}
    for ev in events:
        et = ev["event_type"]
        counts[et] = counts.get(et, 0) + 1
    logger.info("calendar: upserted %d events: %s", len(events), counts)
    return counts
