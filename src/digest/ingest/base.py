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
