"""SEC Form 4 insider trading ingestor.

Reuses the same company list as the EDGAR ingestor (edgar_tickers.yaml) but
filters for Form 4 (Statement of Changes in Beneficial Ownership) and Form 3
filings. Downloads and parses the XML to extract transaction-level detail.

Only generates items for open-market buy/sell transactions (code P/S) above a
minimum dollar threshold — ignores option exercises, gifts, and plan purchases.
"""
from __future__ import annotations

import logging
import defusedxml.ElementTree as ET
from datetime import datetime
from pathlib import Path

import requests
import yaml

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

EDGAR_CONFIG = Path(__file__).resolve().parents[3] / "config" / "edgar_tickers.yaml"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_BASE = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_dashes}/"

# Only surface open-market transactions above this threshold
MIN_TRANSACTION_VALUE = 100_000  # USD

# Codes: P=open-market buy, S=open-market sell. All others skipped.
OPEN_MARKET_CODES = {"P", "S"}

# Topic hint by ticker (falls through to default if not found)
SEMIS_TICKERS = {"NVDA", "AMD", "TSM", "INTC", "AVGO", "QCOM", "ASML"}


def _topic_for(ticker: str) -> str:
    return "ai_semis" if ticker.upper() in SEMIS_TICKERS else "ai_capex"


def _parse_form4(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.debug("insider: XML parse error: %s", exc)
        return []

    issuer_ticker = root.findtext(".//issuerTradingSymbol", "").strip()
    issuer_name = root.findtext(".//issuerName", "").strip()
    owner_name = root.findtext(".//rptOwnerName", "").strip()
    is_officer = root.findtext(".//isOfficer", "0").strip() == "1"
    is_director = root.findtext(".//isDirector", "0").strip() == "1"
    officer_title = root.findtext(".//officerTitle", "").strip()
    role = officer_title if is_officer else ("Director" if is_director else "10%+ Owner")

    txns = []
    for txn in root.findall(".//nonDerivativeTransaction"):
        code = txn.findtext(".//transactionCode", "").strip()
        if code not in OPEN_MARKET_CODES:
            continue
        date_val = txn.findtext(".//transactionDate/value", "").strip()
        shares_val = txn.findtext(".//transactionShares/value", "0").strip()
        price_val = txn.findtext(".//transactionPricePerShare/value", "0").strip()
        acq_disp = txn.findtext(".//transactionAcquiredDisposedCode/value", "").strip()
        post_val = txn.findtext(".//sharesOwnedFollowingTransaction/value", "").strip()
        try:
            shares = float(shares_val.replace(",", ""))
            price = float(price_val.replace(",", ""))
        except ValueError:
            continue
        value = shares * price
        if value < MIN_TRANSACTION_VALUE:
            continue
        txns.append({
            "issuer": issuer_name,
            "ticker": issuer_ticker or "",
            "owner": owner_name,
            "role": role,
            "date": date_val,
            "shares": shares,
            "price": price,
            "value": value,
            "code": code,
            "acq_disp": acq_disp,
            "post_shares": post_val,
        })
    return txns


class InsiderIngestor(IngestorBase):
    name = "insider"

    def __init__(self) -> None:
        if not settings.edgar_user_agent:
            raise RuntimeError("EDGAR_USER_AGENT not set")
        self.headers = {"User-Agent": settings.edgar_user_agent}
        self.config = yaml.safe_load(EDGAR_CONFIG.read_text())

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        for entry in self.config["companies"]:
            cik = str(entry["cik"]).zfill(10)
            ticker = entry["ticker"]
            name = entry.get("name", ticker)
            try:
                r = requests.get(
                    SUBMISSIONS_URL.format(cik=cik), headers=self.headers, timeout=20
                )
                r.raise_for_status()
                data = r.json()
                recent = data.get("filings", {}).get("recent", {})
                forms = recent.get("form", [])
                dates = recent.get("filingDate", [])
                accessions = recent.get("accessionNumber", [])
                primary_docs = recent.get("primaryDocument", [])

                cik_int = str(int(cik))
                for i, form in enumerate(forms[:60]):
                    if form not in ("4", "4/A"):
                        continue
                    accession = accessions[i]
                    filing_date = dates[i]
                    primary_doc = primary_docs[i]

                    accession_nodashes = accession.replace("-", "")
                    # primaryDocument may include an XSL stylesheet prefix
                    # (e.g. "xslF345X06/wk-form4.xml"); the actual file is
                    # always at the root of the accession directory.
                    doc_file = primary_doc.split("/")[-1]
                    doc_url = (
                        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/"
                        f"{accession_nodashes}/{doc_file}"
                    )

                    try:
                        doc_r = requests.get(doc_url, headers=self.headers, timeout=15)
                        doc_r.raise_for_status()
                        xml_text = doc_r.text
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("insider: failed to fetch %s: %s", doc_url, exc)
                        continue

                    txns = _parse_form4(xml_text)
                    for txn in txns:
                        action = "bought" if txn["acq_disp"] == "A" else "sold"
                        value_m = txn["value"] / 1_000_000
                        role = txn["role"]
                        shares_k = txn["shares"] / 1000

                        title = (
                            f"{ticker} insider {action} "
                            f"${value_m:.1f}M ({shares_k:.1f}k shares @ ${txn['price']:.2f}) — "
                            f"{txn['owner']} ({role})"
                        )
                        content = (
                            f"{txn['owner']} ({role} at {name}) {action} {txn['shares']:,.0f} shares "
                            f"of {ticker} at ${txn['price']:.2f}/share on {txn['date']}.\n"
                            f"Total value: ${txn['value']:,.0f}.\n"
                        )
                        if txn["post_shares"]:
                            try:
                                post_k = float(txn["post_shares"].replace(",", "")) / 1000
                                content += f"Post-transaction ownership: {post_k:.1f}k shares."
                            except ValueError:
                                pass

                        try:
                            pub = datetime.strptime(filing_date, "%Y-%m-%d")
                        except ValueError:
                            pub = None

                        items.append(IngestedItem(
                            source=self.name,
                            source_id=f"{accession}:{txn['date']}:{txn['owner'][:20]}",
                            title=title,
                            url=f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=4",
                            content=content,
                            published_at=pub,
                            metadata={
                                "ticker": ticker,
                                "owner": txn["owner"],
                                "role": role,
                                "action": action,
                                "value_usd": txn["value"],
                                "shares": txn["shares"],
                                "price": txn["price"],
                                "topic_hint": _topic_for(ticker),
                            },
                        ))
            except Exception as exc:  # noqa: BLE001
                logger.warning("insider: failed on %s: %s", ticker, exc)

        return items
