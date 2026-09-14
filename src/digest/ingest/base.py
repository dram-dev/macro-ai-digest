"""Shared types and base class for ingestors.

The framework persist→log skeleton and the `IngestedItem` dataclass live in
`digest_core`. This module is the macro-domain seam: it binds the macro `db`
module as the `ItemStore` and re-exports `IngestedItem`, so all 16 ingestors
keep importing `from digest.ingest.base import IngestedItem, IngestorBase`
unchanged.

Because the core base auto-registers every concrete subclass (see
`digest_core.ingest.registry`), each ingestor self-registers in the source
catalog just by existing — no central INGESTORS dict to maintain.
"""
from __future__ import annotations

from digest_core.ingest import IngestedItem, IngestorBase as _CoreIngestorBase

from digest import db

__all__ = ["IngestedItem", "IngestorBase"]


class IngestorBase(_CoreIngestorBase, register=False):
    """macro-domain base: binds the macro SQLite store onto the core skeleton.

    `register=False` keeps this intermediate base out of the catalog; concrete
    subclasses (RSSIngestor, FREDIngestor, …) register automatically.
    """

    store = db

    def enrich_items(self, items: list[IngestedItem]) -> list[IngestedItem]:
        """Expand excerpt-only feed entries into full article text.

        Bound here rather than in each ingestor so any feed-backed source opts
        in with one class attribute. Items already stored are skipped: the fetch
        is the expensive part and `upsert_items` would discard the result.
        """
        if not self.enrich_fulltext:
            return items
        from digest.ingest.fulltext import enrich

        seen = db.existing_source_ids(self.name)
        for item in items:
            url = self.enrich_url(item)
            if url and item.source_id not in seen:
                item.content = enrich(item.content, url)
        return items
