-- macro-ai-digest gold layer — `macro_gold` views in the shared `digest`
-- catalog. Only the views supportable from macro's domain-agnostic tables are
-- shipped today: source_quality (ingest ⋈ triage ⋈ summary) and pipeline_slos
-- (telemetry). Leaderboard/calibration views await macro's per-item scores +
-- ratings landing in the lakehouse (signals-seam work).
--
-- Apply after macro_bronze + macro_silver DDL.

CREATE SCHEMA IF NOT EXISTS macro_gold;

-- Per-source quality — keep rate + avg materiality. Surfaces noisy sources.
CREATE OR REPLACE VIEW macro_gold.source_quality AS
SELECT
    b.source,
    DATE(b.ingested_at)                                            AS day,
    COUNT(*)                                                        AS items_ingested,
    COUNT_IF(t.decision = 'keep')                                   AS items_kept,
    COUNT_IF(t.decision = 'drop')                                   AS items_dropped,
    ROUND(COUNT_IF(t.decision = 'keep') / NULLIF(COUNT(*), 0), 3)   AS keep_rate,
    AVG(CASE WHEN t.decision = 'keep' THEN sm.materiality END)      AS avg_materiality
FROM macro_bronze.ingested_items b
LEFT JOIN macro_silver.triage_verdicts t  USING (item_hash)
LEFT JOIN macro_silver.summaries sm       USING (item_hash)
GROUP BY b.source, DATE(b.ingested_at);

-- Operational SLOs by source + stage. Watch this when a feed silently degrades.
CREATE OR REPLACE VIEW macro_gold.pipeline_slos AS
SELECT
    stage,
    source,
    DATE(started_at)                                               AS day,
    COUNT(*)                                                        AS runs,
    SUM(items_in)                                                   AS items_in,
    SUM(items_out)                                                  AS items_out,
    AVG(duration_ms)                                                AS avg_duration_ms,
    PERCENTILE(duration_ms, 0.95)                                   AS p95_duration_ms,
    SUM(errors)                                                     AS total_errors
FROM macro_bronze.pipeline_telemetry
GROUP BY stage, source, DATE(started_at);

-- Cross-domain hook (optional): once both pc_* and macro_* are populated, a
-- cross-domain view can union source_quality / leaderboards across schemas to
-- correlate macro CPI/rates signals with PC loss-cost/regulatory signals
-- (the `macro_linkage` topic). Left to the analytics option (Option 2).
