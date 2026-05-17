"""arXiv ingestor — pulls recent AI/NLP papers from arXiv category RSS feeds.

Feeds update once per day Mon–Fri after ~8 PM ET; weekends return 0 entries,
which is handled gracefully (0 new items, no error).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from time import mktime

import feedparser

from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

# cs.AI: Artificial Intelligence; cs.CL: Computation and Language (LLMs/NLP)
ARXIV_FEEDS = [
    "https://arxiv.org/rss/cs.AI",
    "https://arxiv.org/rss/cs.CL",
]

# Limit per category feed so we don't overwhelm triage with marginal papers.
LIMIT_PER_FEED = 25


def _paper_id(entry_id: str) -> str:
    """Extract 'YYMM.NNNNN' from 'http://arxiv.org/abs/2405.12345v1'."""
    m = re.search(r"abs/([0-9.]+)", entry_id)
    return m.group(1) if m else entry_id


def _entry_date(entry: dict) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        st = entry.get(key)
        if st:
            return datetime.fromtimestamp(mktime(st))
    return None


class ArXivIngestor(IngestorBase):
    name = "arxiv"

    def fetch(self) -> list[IngestedItem]:
        items: list[IngestedItem] = []
        seen: set[str] = set()  # dedup across categories
        for feed_url in ARXIV_FEEDS:
            try:
                parsed = feedparser.parse(feed_url)
                if parsed.bozo:
                    logger.warning("arxiv: %s bozo=%s", feed_url, parsed.bozo_exception)
                for entry in parsed.entries[:LIMIT_PER_FEED]:
                    raw_id = entry.get("id", "")
                    paper_id = _paper_id(raw_id)
                    if paper_id in seen:
                        continue
                    seen.add(paper_id)
                    title = re.sub(r"\s+", " ", entry.get("title", "(no title)")).strip()
                    abstract = re.sub(r"\s+", " ", entry.get("summary", "")).strip()
                    authors = entry.get("authors", [])
                    first_author = authors[0].get("name") if authors else None
                    abs_url = entry.get("link") or f"https://arxiv.org/abs/{paper_id}"
                    items.append(
                        IngestedItem(
                            source=self.name,
                            source_id=paper_id,
                            title=title,
                            url=abs_url,
                            author=first_author,
                            content=abstract,
                            published_at=_entry_date(entry),
                            metadata={
                                "paper_id": paper_id,
                                "all_authors": [a.get("name") for a in authors[:5]],
                                "topic_hint": "ai_thinkers",
                            },
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("arxiv: failed on %s: %s", feed_url, exc)
        return items
