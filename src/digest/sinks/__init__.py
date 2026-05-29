"""Pipeline sinks — secondary write destinations alongside SQLite.

The Databricks sink implementation lives in `digest_core.sinks.databricks`
(shared with pc-insurance-digest); this module constructs the singleton from
macro's settings. macro writes the domain-agnostic medallion tables
(macro_bronze.ingested_items / pipeline_telemetry, macro_silver.triage_verdicts /
summaries) into the shared `digest` catalog. Regime + signal_scores stay
SQLite-only for now — macro's schemas there diverge from PC's (1-axis week-keyed
regime; different score factors), pending the signals-seam design.
"""
from __future__ import annotations

from digest.config import settings
from digest_core.sinks.databricks import DatabricksSink, item_hash

sink: DatabricksSink = DatabricksSink(
    enabled=settings.databricks_enabled,
    host=settings.databricks_host,
    http_path=settings.databricks_http_path,
    token=settings.databricks_token,
    catalog=settings.databricks_catalog,
    schema_prefix=settings.databricks_schema_prefix,
)

__all__ = ["DatabricksSink", "item_hash", "sink"]
