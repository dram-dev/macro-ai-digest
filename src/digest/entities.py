"""Entity extraction → ticker linkage via dictionary lookup (Feature 3).

No spaCy or NLP dependencies. A curated ~200-entry dictionary covers the
major financial entities, central banks, key people, and indices.
Extracted entities stored as entities_json on items.

Run via: digest entities
"""
from __future__ import annotations

import json
import logging
import re

from digest import db

logger = logging.getLogger(__name__)

# ── Entity dictionary {name: {ticker, type}} ───────────────────────────
# Longest names listed first in _SORTED_ENTITIES to avoid partial-match clobber.

ENTITY_DICT: dict[str, dict] = {
    # Central banks / policy bodies
    "Federal Reserve":          {"ticker": None,      "type": "central_bank"},
    "FOMC":                     {"ticker": None,      "type": "central_bank"},
    "Fed":                      {"ticker": None,      "type": "central_bank"},
    "Jerome Powell":            {"ticker": None,      "type": "person"},
    "European Central Bank":    {"ticker": None,      "type": "central_bank"},
    "ECB":                      {"ticker": None,      "type": "central_bank"},
    "Bank of Japan":            {"ticker": None,      "type": "central_bank"},
    "BOJ":                      {"ticker": None,      "type": "central_bank"},
    "Bank of England":          {"ticker": None,      "type": "central_bank"},
    "BOE":                      {"ticker": None,      "type": "central_bank"},
    "People's Bank of China":   {"ticker": None,      "type": "central_bank"},
    "PBOC":                     {"ticker": None,      "type": "central_bank"},
    # Tech / AI
    "Alphabet":                 {"ticker": "GOOGL",   "type": "company"},
    "Microsoft":                {"ticker": "MSFT",    "type": "company"},
    "NVIDIA":                   {"ticker": "NVDA",    "type": "company"},
    "Nvidia":                   {"ticker": "NVDA",    "type": "company"},
    "Amazon":                   {"ticker": "AMZN",    "type": "company"},
    "Apple":                    {"ticker": "AAPL",    "type": "company"},
    "Google":                   {"ticker": "GOOGL",   "type": "company"},
    "Meta":                     {"ticker": "META",    "type": "company"},
    "Tesla":                    {"ticker": "TSLA",    "type": "company"},
    "OpenAI":                   {"ticker": None,      "type": "company"},
    "Anthropic":                {"ticker": None,      "type": "company"},
    "xAI":                      {"ticker": None,      "type": "company"},
    "Broadcom":                 {"ticker": "AVGO",    "type": "company"},
    "AMD":                      {"ticker": "AMD",     "type": "company"},
    "Intel":                    {"ticker": "INTC",    "type": "company"},
    "TSMC":                     {"ticker": "TSM",     "type": "company"},
    "Qualcomm":                 {"ticker": "QCOM",    "type": "company"},
    "Samsung":                  {"ticker": None,      "type": "company"},
    "IBM":                      {"ticker": "IBM",     "type": "company"},
    "Oracle":                   {"ticker": "ORCL",    "type": "company"},
    "Salesforce":               {"ticker": "CRM",     "type": "company"},
    "Palantir":                 {"ticker": "PLTR",    "type": "company"},
    "Snowflake":                {"ticker": "SNOW",    "type": "company"},
    "Databricks":               {"ticker": None,      "type": "company"},
    "Scale AI":                 {"ticker": None,      "type": "company"},
    "Hugging Face":             {"ticker": None,      "type": "company"},
    "DeepMind":                 {"ticker": None,      "type": "company"},
    "Mistral":                  {"ticker": None,      "type": "company"},
    "C3.ai":                    {"ticker": "AI",      "type": "company"},
    "ServiceNow":               {"ticker": "NOW",     "type": "company"},
    "Workday":                  {"ticker": "WDAY",    "type": "company"},
    "CrowdStrike":              {"ticker": "CRWD",    "type": "company"},
    "Palo Alto Networks":       {"ticker": "PANW",    "type": "company"},
    "Cloudflare":               {"ticker": "NET",     "type": "company"},
    # Finance / banks
    "JPMorgan Chase":           {"ticker": "JPM",     "type": "company"},
    "JPMorgan":                 {"ticker": "JPM",     "type": "company"},
    "JP Morgan":                {"ticker": "JPM",     "type": "company"},
    "Goldman Sachs":            {"ticker": "GS",      "type": "company"},
    "Morgan Stanley":           {"ticker": "MS",      "type": "company"},
    "Bank of America":          {"ticker": "BAC",     "type": "company"},
    "Citigroup":                {"ticker": "C",       "type": "company"},
    "Wells Fargo":              {"ticker": "WFC",     "type": "company"},
    "BlackRock":                {"ticker": "BLK",     "type": "company"},
    "Vanguard":                 {"ticker": None,      "type": "fund"},
    "Bridgewater":              {"ticker": None,      "type": "fund"},
    "Citadel":                  {"ticker": None,      "type": "fund"},
    "Renaissance Technologies": {"ticker": None,      "type": "fund"},
    "AQR":                      {"ticker": None,      "type": "fund"},
    "Blackstone":               {"ticker": "BX",      "type": "company"},
    "KKR":                      {"ticker": "KKR",     "type": "company"},
    "Carlyle":                  {"ticker": "CG",      "type": "company"},
    "Apollo Global":            {"ticker": "APO",     "type": "company"},
    "Apollo":                   {"ticker": "APO",     "type": "company"},
    "Charles Schwab":           {"ticker": "SCHW",    "type": "company"},
    "Fidelity":                 {"ticker": None,      "type": "fund"},
    "PIMCO":                    {"ticker": None,      "type": "fund"},
    "Berkshire Hathaway":       {"ticker": "BRK-B",   "type": "company"},
    "Berkshire":                {"ticker": "BRK-B",   "type": "company"},
    # Energy
    "ExxonMobil":               {"ticker": "XOM",     "type": "company"},
    "Exxon":                    {"ticker": "XOM",     "type": "company"},
    "Chevron":                  {"ticker": "CVX",     "type": "company"},
    "Shell":                    {"ticker": "SHEL",    "type": "company"},
    "BP":                       {"ticker": "BP",      "type": "company"},
    "ConocoPhillips":           {"ticker": "COP",     "type": "company"},
    "Halliburton":              {"ticker": "HAL",     "type": "company"},
    "Schlumberger":             {"ticker": "SLB",     "type": "company"},
    "OPEC":                     {"ticker": None,      "type": "organization"},
    # Indices / ETFs
    "S&P 500":                  {"ticker": "SPY",     "type": "index"},
    "Nasdaq 100":               {"ticker": "QQQ",     "type": "index"},
    "Nasdaq":                   {"ticker": "QQQ",     "type": "index"},
    "Dow Jones":                {"ticker": "DIA",     "type": "index"},
    "Russell 2000":             {"ticker": "IWM",     "type": "index"},
    "VIX":                      {"ticker": None,      "type": "index"},
    "Emerging Markets":         {"ticker": "EEM",     "type": "region"},
    # Key people
    "Elon Musk":                {"ticker": None,      "type": "person"},
    "Sam Altman":               {"ticker": None,      "type": "person"},
    "Jensen Huang":             {"ticker": None,      "type": "person"},
    "Larry Fink":               {"ticker": None,      "type": "person"},
    "Jamie Dimon":              {"ticker": None,      "type": "person"},
    "Janet Yellen":             {"ticker": None,      "type": "person"},
    "Warren Buffett":           {"ticker": "BRK-B",   "type": "person"},
    "Ray Dalio":                {"ticker": None,      "type": "person"},
    "Cathie Wood":              {"ticker": None,      "type": "person"},
    "Dario Amodei":             {"ticker": None,      "type": "person"},
    "Mark Zuckerberg":          {"ticker": None,      "type": "person"},
    "Satya Nadella":            {"ticker": None,      "type": "person"},
    "Tim Cook":                 {"ticker": None,      "type": "person"},
    "Sundar Pichai":            {"ticker": None,      "type": "person"},
    "Michael Burry":            {"ticker": None,      "type": "person"},
    "David Sacks":              {"ticker": None,      "type": "person"},
    # Organizations / regulators
    "Treasury":                 {"ticker": None,      "type": "organization"},
    "SEC":                      {"ticker": None,      "type": "organization"},
    "FDIC":                     {"ticker": None,      "type": "organization"},
    "IMF":                      {"ticker": None,      "type": "organization"},
    "World Bank":               {"ticker": None,      "type": "organization"},
    "BIS":                      {"ticker": None,      "type": "organization"},
    "WTO":                      {"ticker": None,      "type": "organization"},
    # Countries / regions
    "China":                    {"ticker": None,      "type": "country"},
    "Japan":                    {"ticker": None,      "type": "country"},
    "Europe":                   {"ticker": None,      "type": "region"},
    "Eurozone":                 {"ticker": None,      "type": "region"},
    # Crypto
    "Bitcoin":                  {"ticker": "BTC-USD", "type": "crypto"},
    "Ethereum":                 {"ticker": "ETH-USD", "type": "crypto"},
}

_SORTED_ENTITIES = sorted(ENTITY_DICT.keys(), key=len, reverse=True)

# Reverse map: ticker → (canonical name, entity info) — used for ticker-symbol second pass.
# When multiple names share a ticker (NVIDIA/Nvidia → NVDA), keep the longest name.
_TICKER_TO_ENTITY: dict[str, tuple[str, dict]] = {}
for _name, _info in ENTITY_DICT.items():
    _t = _info.get("ticker")
    if _t and (_t not in _TICKER_TO_ENTITY or len(_name) > len(_TICKER_TO_ENTITY[_t][0])):
        _TICKER_TO_ENTITY[_t] = (_name, _info)


def extract_entities(text: str) -> list[dict]:
    """Return list of {name, ticker, type} for every entity found in text."""
    found: list[dict] = []
    seen_names: set[str] = set()
    seen_tickers: set[str] = set()

    for name in _SORTED_ENTITIES:
        if name in seen_names:
            continue
        pattern = r"\b" + re.escape(name) + r"\b"
        if not re.search(pattern, text, re.IGNORECASE):
            continue
        info = ENTITY_DICT[name]
        ticker = info["ticker"]
        if ticker and ticker in seen_tickers:
            seen_names.add(name)
            continue
        found.append({"name": name, "ticker": ticker, "type": info["type"]})
        seen_names.add(name)
        if ticker:
            seen_tickers.add(ticker)

    # Second pass: catch bare ticker symbols (e.g. "NVDA") not matched by name above.
    for ticker, (canonical_name, info) in _TICKER_TO_ENTITY.items():
        if ticker in seen_tickers:
            continue
        if re.search(r"\b" + re.escape(ticker) + r"\b", text):
            found.append({"name": canonical_name, "ticker": ticker, "type": info["type"]})
            seen_tickers.add(ticker)
            seen_names.add(canonical_name)

    return found


def run_entities(limit: int = 500) -> dict[str, int]:
    """Extract entities for kept items lacking entities_json. Returns counts."""
    rows = db.items_needing_entities(limit=limit)
    counts = {"processed": 0, "with_entities": 0}
    for row in rows:
        counts["processed"] += 1
        text = " ".join(filter(None, [
            row["title"], row["summary"], row["why_it_matters"]
        ]))
        entities = extract_entities(text)
        db.update_entities(row["id"], json.dumps(entities))
        if entities:
            counts["with_entities"] += 1
    return counts
