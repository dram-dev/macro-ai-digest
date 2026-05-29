-- macro-ai-digest bronze layer — raw firehose, shared `digest` catalog,
-- `macro_bronze` schema (pc-insurance-digest uses pc_*; both live in one
-- catalog for cross-domain queries). The sink's DATABRICKS_SCHEMA_PREFIX
-- (macro_) must match these schema names.
--
-- Scope note: macro currently writes the DOMAIN-AGNOSTIC tables only
-- (ingested_items, pipeline_telemetry; triage_verdicts + summaries in silver).
-- macro's regime (1-axis, ISO-week-keyed) and per-item scores diverge from PC's
-- shapes and stay SQLite-only pending the digest-core signals/regime seam.
--
-- Join key: item_hash = sha256(source || '::' || source_id), derived at
-- sink-write time. SQLite stays the source of truth.
--
-- Apply with: USE CATALOG digest; then run this file.

CREATE SCHEMA IF NOT EXISTS macro_bronze;

-- Every IngestedItem from every source, INCLUDING triage drops (they train the
-- triage-quality dashboards).
CREATE TABLE IF NOT EXISTS macro_bronze.ingested_items (
    item_hash      STRING  NOT NULL,
    source         STRING  NOT NULL,
    source_id      STRING  NOT NULL,
    url            STRING,
    title          STRING  NOT NULL,
    author         STRING,
    content        STRING,
    published_at   TIMESTAMP,
    ingested_at    TIMESTAMP NOT NULL,
    metadata_json  STRING,
    topic_hint     STRING,
    CONSTRAINT macro_bronze_ingested_items_pk PRIMARY KEY (item_hash)
)
USING DELTA
PARTITIONED BY (source);

-- Operational telemetry per pipeline stage — source-level SLO dashboards.
-- Subsumes SQLite run_log + summarizer_log.
CREATE TABLE IF NOT EXISTS macro_bronze.pipeline_telemetry (
    run_id        STRING NOT NULL,
    stage         STRING NOT NULL,    -- ingest|triage|summarize|publish|signals
    source        STRING,
    started_at    TIMESTAMP NOT NULL,
    ended_at      TIMESTAMP NOT NULL,
    duration_ms   BIGINT NOT NULL,
    items_in      INT,
    items_out     INT,
    errors        INT NOT NULL,
    error_detail  STRING,
    model_id      STRING,
    CONSTRAINT macro_bronze_telemetry_pk PRIMARY KEY (run_id, stage, source)
)
USING DELTA
PARTITIONED BY (stage);
