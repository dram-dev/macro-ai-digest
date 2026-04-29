"""SEC EDGAR ingestor — pulls recent filings for hyperscaler capex tracking.

Uses the public EDGAR submissions API (no auth). SEC requires a user-agent with
a real name and email; set EDGAR_USER_AGENT in .env.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import requests
import yaml

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

EDGAR_CONFIG = Path(__file__).resolve().parents[3] / "config" / "edgar_tickers.yaml"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{primary_doc}"

# Filing types we care about for capex signal
RELEVANT_FORMS = {"10-K", "10-Q", "8-K"}


class EdgarIngestor(IngestorBase):
    name = "edgar"

    def __init__(self) -> None:
        if not settings.edgar_user_agent:
            raise RuntimeError(
                "EDGAR_USER_AGENT not set. SEC requires a user-agent like "
                "'Your Name your.email@example.com'."
            )
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

                for i, form in enumerate(forms[:40]):  # window on recent 40 filings
                    if form not in RELEVANT_FORMS:
                        continue
                    accession = accessions[i]
                    filing_date = dates[i]
                    primary_doc = primary_docs[i]

                    cik_int = str(int(cik))
                    accession_dashes = accession.replace("-", "")
                    url = FILING_URL.format(
                        cik_int=cik_int,
                        accession_no_dashes=accession_dashes,
                        primary_doc=primary_doc,
                    )

                    try:
                        published = datetime.strptime(filing_date, "%Y-%m-%d")
                    except ValueError:
                        published = None

                    items.append(
                        IngestedItem(
                            source=self.name,
                            source_id=f"{ticker}:{accession}",
                            title=f"{ticker} {form} filed {filing_date}",
                            url=url,
                            author=name,
                            content=None,  # Phase 2 will fetch/parse the doc body
                            published_at=published,
                            metadata={
                                "ticker": ticker,
                                "cik": cik,
                                "form": form,
                                "accession": accession,
                                "primary_document": primary_doc,
                            },
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("edgar: failed on %s (CIK %s): %s", ticker, cik, exc)
        return items
