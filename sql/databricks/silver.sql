-- macro-ai-digest silver layer — `macro_silver` schema in the shared `digest`
-- catalog. Domain-agnostic tables only (see bronze.sql scope note). Apply after
-- bronze (macro_bronze) DDL.

CREATE SCHEMA IF NOT EXISTS macro_silver;

-- Triage verdict per item per triage run. macro's triage emits decision/score/
-- topic; burden_* / sub_tags columns are present for shape-compatibility with
-- PC but stay NULL for macro.
CREATE TABLE IF NOT EXISTS macro_silver.triage_verdicts (
    item_hash         STRING    NOT NULL,
    triaged_at        TIMESTAMP NOT NULL,
    decision          STRING    NOT NULL,        -- 'keep'|'drop'
    score             DOUBLE,
    topic             STRING,
    sub_tags          ARRAY<STRING>,
    confidence        STRING,
    reason            STRING,
    burden_direction  STRING,
    burden_intensity  STRING,
    model_id          STRING,
    CONSTRAINT macro_silver_triage_pk PRIMARY KEY (item_hash, triaged_at)
)
USING DELTA;

-- Summarizer output. macro's SummaryOutput has no materiality; the column is
-- present (nullable) for shape-compatibility with PC.
CREATE TABLE IF NOT EXISTS macro_silver.summaries (
    item_hash       STRING    NOT NULL,
    summarized_at   TIMESTAMP NOT NULL,
    summary         STRING    NOT NULL,
    why_it_matters  STRING,
    see_also        STRING,
    materiality     DOUBLE,
    confidence      STRING,
    input_chars     INT,
    output_chars    INT,
    model_id        STRING,
    duration_ms     BIGINT,
    CONSTRAINT macro_silver_summaries_pk PRIMARY KEY (item_hash, summarized_at)
)
USING DELTA;
