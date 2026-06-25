"""SQLite schema and helpers. Raw sqlite3 — no ORM, keeps things boring.

The domain-agnostic base (items/run_log/summarizer_log schema, connection
management, and the generic CRUD helpers) lives in `digest_core.db`. This
module is the macro-domain layer on top: it owns the macro-specific migrations
(macro_regime, daily_connections, signal_outcomes, upcoming_events, plus the
ensemble/sentiment/entity/cluster columns) and the many domain query helpers
below. The thin wrappers default `db_path` from settings and delegate to core,
so public signatures here are unchanged across the digest-core lift.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from digest_core.db import helpers as core_db
from digest_core.types import IngestedItem

from digest.config import settings
from digest.sinks import sink

# Domain migrations layered on digest_core's BASE_SCHEMA (items / run_log /
# summarizer_log). Applied idempotently by core_db.init_db_with_migrations,
# which swallows "duplicate column" errors — so the ALTERs that re-add columns
# already present in BASE_SCHEMA (confidence, see_also, triage_*, summarized_at)
# are no-ops on a fresh DB and real migrations on a pre-lift one.
MIGRATIONS = [
    # fred_baseline predates the lift and is macro-only (FRED anomaly z-score
    # baselines), so it moved out of the shared base schema into this list.
    """CREATE TABLE IF NOT EXISTS fred_baseline (
        series_id       TEXT PRIMARY KEY,
        mean_delta      REAL,
        stddev_delta    REAL,
        updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "ALTER TABLE items ADD COLUMN confidence TEXT",
    "ALTER TABLE items ADD COLUMN see_also TEXT",
    "ALTER TABLE items ADD COLUMN triage_score REAL",
    "ALTER TABLE items ADD COLUMN triage_decision TEXT",
    "ALTER TABLE items ADD COLUMN triaged_at TEXT",
    "ALTER TABLE items ADD COLUMN summarized_at TEXT",
    "ALTER TABLE items ADD COLUMN obsidian_written_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_items_triage ON items(triage_decision)",
    "CREATE INDEX IF NOT EXISTS idx_items_obsidian ON items(obsidian_written_at)",
    # Phase 4: connection threads + weekly synthesis
    """CREATE TABLE IF NOT EXISTS daily_connections (
        date         TEXT PRIMARY KEY,
        threads_json TEXT NOT NULL,
        generated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    # Phase 5: macro regime classifier
    """CREATE TABLE IF NOT EXISTS macro_regime (
        week         TEXT PRIMARY KEY,
        regime       TEXT NOT NULL,
        signals_json TEXT NOT NULL,
        narrative    TEXT NOT NULL,
        generated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    # Idea 3: multi-persona ensemble scores
    "ALTER TABLE items ADD COLUMN ensemble_scores TEXT",
    "ALTER TABLE items ADD COLUMN ensemble_consensus REAL",
    "ALTER TABLE items ADD COLUMN ensemble_dispersion REAL",
    # Idea 1: TF-IDF narrative cluster label
    "ALTER TABLE items ADD COLUMN cluster_id TEXT",
    # Idea 2: signal outcome tracking
    """CREATE TABLE IF NOT EXISTS signal_outcomes (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id      INTEGER NOT NULL REFERENCES items(id),
        checked_at   TEXT NOT NULL DEFAULT (datetime('now')),
        horizon_days INTEGER NOT NULL DEFAULT 7,
        outcome      TEXT NOT NULL,
        original_z   REAL,
        followup_z   REAL,
        magnitude    REAL,
        UNIQUE(item_id, horizon_days)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_outcomes_item ON signal_outcomes(item_id)",
    # Feature 1: financial sentiment
    "ALTER TABLE items ADD COLUMN sentiment_label TEXT",
    "ALTER TABLE items ADD COLUMN sentiment_score REAL",
    # Feature 3: entity extraction
    "ALTER TABLE items ADD COLUMN entities_json TEXT",
    # Feature 2: forward event calendar
    """CREATE TABLE IF NOT EXISTS upcoming_events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type    TEXT NOT NULL,
        event_date    TEXT NOT NULL,
        title         TEXT NOT NULL,
        symbol        TEXT,
        metadata_json TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(event_type, event_date, title)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_events_date ON upcoming_events(event_date)",
    # Wave 2: persistent storylines (multi-day narrative threading)
    """CREATE TABLE IF NOT EXISTS storylines (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        slug        TEXT NOT NULL UNIQUE,
        name        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'active',
        state       TEXT NOT NULL,
        resolution  TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        last_moved  TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS storyline_deltas (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        storyline_id INTEGER NOT NULL REFERENCES storylines(id),
        date         TEXT NOT NULL,
        delta        TEXT NOT NULL,
        item_ids     TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(storyline_id, date)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_storyline_deltas_date ON storyline_deltas(date)",
    # Wave 3: prediction scorecard (falsifiable calls from essays/debates/weeklies)
    """CREATE TABLE IF NOT EXISTS predictions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        source       TEXT NOT NULL,
        source_ref   TEXT NOT NULL,
        made_on      TEXT NOT NULL,
        due_on       TEXT NOT NULL,
        claim        TEXT NOT NULL,
        observable   TEXT NOT NULL,
        direction    TEXT,
        status       TEXT NOT NULL DEFAULT 'open',
        rationale    TEXT,
        evidence_ids TEXT,
        resolved_on  TEXT,
        created_at   TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(source, source_ref, claim)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_predictions_status ON predictions(status, due_on)",
    # Wave 4: weekly topic state-of-play + LLM cluster names
    """CREATE TABLE IF NOT EXISTS topic_state (
        topic       TEXT PRIMARY KEY,
        state       TEXT NOT NULL,
        changed     TEXT,
        watch       TEXT,
        week        TEXT,
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    """CREATE TABLE IF NOT EXISTS cluster_names (
        cluster_id  TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    # Push-notification dedup log — one row per alert ever sent, keyed so the
    # same signal never re-fires (covers am/pm double-runs permanently).
    """CREATE TABLE IF NOT EXISTS notify_log (
        alert_key   TEXT PRIMARY KEY,
        kind        TEXT NOT NULL,
        item_id     INTEGER,
        sent_at     TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
]


def init_db(db_path: Path | None = None) -> None:
    """Create DB file + base schema if missing; apply macro migrations idempotently."""
    core_db.init_db_with_migrations(db_path or settings.db_path, MIGRATIONS)


def get_conn(db_path: Path | None = None) -> AbstractContextManager[sqlite3.Connection]:
    """Connection context manager (row factory + WAL); defaults to the configured DB."""
    return core_db.get_conn(db_path or settings.db_path)


def upsert_items(items: Iterable[IngestedItem]) -> int:
    """Insert new items, ignore duplicates. Returns count of new rows."""
    items_list = list(items)
    if not items_list:
        return 0
    with get_conn() as conn:
        inserted = core_db.upsert_items(conn, items_list)
    # Bronze sink: every ingested item, including soon-to-be-dropped ones.
    sink.write_ingested(items_list)
    return inserted


def log_run(
    run_type: str,
    source: str,
    items_fetched: int,
    items_new: int,
    duration_ms: int,
    status: str,
    error: str | None = None,
) -> None:
    """Append a row to run_log."""
    with get_conn() as conn:
        core_db.log_run(
            conn, run_type, source, items_fetched, items_new, duration_ms, status, error
        )
    # Bronze telemetry (stage=ingest) — derive started_at from duration.
    ended = datetime.now(timezone.utc)
    started = ended - timedelta(milliseconds=duration_ms)
    sink.write_telemetry({
        "run_id":       f"{started.isoformat(timespec='seconds')}-{source}",
        "stage":        "ingest",
        "source":       source,
        "started_at":   started.isoformat(timespec="seconds"),
        "ended_at":     ended.isoformat(timespec="seconds"),
        "duration_ms":  duration_ms,
        "items_in":     items_fetched,
        "items_out":    items_new,
        "errors":       0 if status == "ok" else 1,
        "error_detail": error,
        "model_id":     None,
    })


def item_stats() -> dict[str, int]:
    """Return item counts grouped by source."""
    with get_conn() as conn:
        return core_db.item_stats(conn)


def recent_items(source: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
    """Return most recently ingested items, optionally filtered by source."""
    with get_conn() as conn:
        return core_db.recent_items(conn, source, limit)


# Re-exported from digest_core so callers can keep importing it from digest.db.
utcnow_iso = core_db.utcnow_iso


# ── Phase 2 helpers ────────────────────────────────────────────────────


def items_needing_triage(limit: int = 200) -> list[sqlite3.Row]:
    """Items ingested within the lookback window with no triage decision yet."""
    lookback = f"-{settings.triage_lookback_hours} hours"
    sql = """
        SELECT id, source, source_id, url, title, author, content,
               published_at, metadata_json
        FROM items
        WHERE triage_decision IS NULL
          AND ingested_at >= datetime('now', ?)
        ORDER BY ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (lookback, limit)).fetchall()


def items_for_signals() -> list[sqlite3.Row]:
    """All summarized, kept items for signal scoring (no limit — scored in Python)."""
    sql = """
        SELECT id, source, url, title, author,
               published_at, ingested_at,
               topic, summary, why_it_matters, confidence, see_also,
               triage_score, metadata_json,
               ensemble_consensus, ensemble_dispersion, cluster_id,
               sentiment_label, sentiment_score
        FROM items
        WHERE triage_decision = 'keep'
          AND summary IS NOT NULL
        ORDER BY triage_score DESC, ingested_at DESC
    """
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


def recent_kept_titles(hours: int = 24) -> list[str]:
    """Titles of kept items from the last N hours, for near-duplicate detection."""
    sql = """
        SELECT title FROM items
        WHERE triage_decision = 'keep'
          AND triaged_at >= datetime('now', ?)
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (f"-{hours} hours",)).fetchall()
    return [r["title"] for r in rows if r["title"]]


def unnotified_high_signals(min_score: float, limit: int) -> list[sqlite3.Row]:
    """Kept + summarized items scoring >= min_score that haven't been pushed yet.

    A left-join against notify_log excludes anything already alerted, so the
    am and pm runs never re-fire the same signal. Highest score first.
    """
    sql = """
        SELECT i.id, i.source, i.url, i.title, i.topic,
               i.summary, i.why_it_matters, i.triage_score
        FROM items i
        LEFT JOIN notify_log n
               ON n.alert_key = 'signal:' || i.id
        WHERE i.triage_decision = 'keep'
          AND i.summary IS NOT NULL
          AND i.triage_score >= ?
          AND n.alert_key IS NULL
        ORDER BY i.triage_score DESC, i.ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (min_score, limit)).fetchall()


def record_notification(alert_key: str, kind: str, item_id: int | None = None) -> None:
    """Mark an alert as sent so it never re-fires. Idempotent (INSERT OR IGNORE)."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO notify_log (alert_key, kind, item_id) VALUES (?, ?, ?)",
            (alert_key, kind, item_id),
        )


def items_needing_ensemble(limit: int = 200) -> list[sqlite3.Row]:
    """Kept + summarized items with no ensemble score yet."""
    sql = """
        SELECT id, source, title, topic, summary, why_it_matters
        FROM items
        WHERE triage_decision = 'keep'
          AND summary IS NOT NULL
          AND ensemble_consensus IS NULL
        ORDER BY triage_score DESC, ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (limit,)).fetchall()


def update_ensemble(
    item_id: int, scores_json: str, consensus: float, dispersion: float
) -> None:
    with get_conn() as conn:
        conn.execute(
            """UPDATE items
               SET ensemble_scores = ?, ensemble_consensus = ?, ensemble_dispersion = ?
               WHERE id = ?""",
            (scores_json, consensus, dispersion, item_id),
        )


def items_for_clustering() -> list[sqlite3.Row]:
    """All kept+summarized items for TF-IDF clustering."""
    sql = """
        SELECT id, title, summary
        FROM items
        WHERE triage_decision = 'keep'
          AND summary IS NOT NULL
        ORDER BY ingested_at DESC
    """
    with get_conn() as conn:
        return conn.execute(sql).fetchall()


def update_cluster_ids(id_to_label: dict[int, str]) -> None:
    if not id_to_label:
        return
    with get_conn() as conn:
        for item_id, label in id_to_label.items():
            conn.execute(
                "UPDATE items SET cluster_id = ? WHERE id = ?",
                (label, item_id),
            )


_VALID_OUTCOME_KEYS = frozenset({"series_id", "symbol", "contract"})


def items_for_outcome_check(horizon_days: int = 7, limit: int = 500) -> list[sqlite3.Row]:
    """FRED/CBOE/CFTC kept items old enough to check, with no outcome yet for this horizon."""
    sql = """
        SELECT i.id, i.source, i.ingested_at, i.metadata_json
        FROM items i
        LEFT JOIN signal_outcomes so ON so.item_id = i.id AND so.horizon_days = ?
        WHERE i.source IN ('fred', 'cboe', 'cftc')
          AND i.triage_decision = 'keep'
          AND i.ingested_at <= datetime('now', ?)
          AND so.id IS NULL
          AND json_extract(i.metadata_json, '$.z_score') IS NOT NULL
        ORDER BY i.ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (horizon_days, f"-{horizon_days} days", limit)).fetchall()


def get_followup_z(
    source: str, meta_key: str, key_value: str, after_iso: str
) -> float | None:
    """Latest z_score for same series/symbol/contract ingested after a given timestamp."""
    if meta_key not in _VALID_OUTCOME_KEYS:
        raise ValueError(f"Invalid meta_key: {meta_key!r}")
    sql = f"""
        SELECT CAST(json_extract(metadata_json, '$.z_score') AS REAL) AS z_score
        FROM items
        WHERE source = ?
          AND json_extract(metadata_json, '$.{meta_key}') = ?
          AND ingested_at > ?
          AND json_extract(metadata_json, '$.z_score') IS NOT NULL
        ORDER BY ingested_at DESC
        LIMIT 1
    """
    with get_conn() as conn:
        row = conn.execute(sql, (source, key_value, after_iso)).fetchone()
    return float(row["z_score"]) if row else None


def upsert_outcome(
    item_id: int,
    horizon_days: int,
    outcome: str,
    original_z: float | None,
    followup_z: float | None,
    magnitude: float | None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO signal_outcomes
               (item_id, horizon_days, outcome, original_z, followup_z, magnitude)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (item_id, horizon_days, outcome, original_z, followup_z, magnitude),
        )


def recently_resolved_outcomes(hours: int = 36) -> list[sqlite3.Row]:
    """Quant-signal outcomes that resolved (non-pending) in the last N hours.

    Joined to item title/url/source so the daily Brief can show a scoreboard
    of which past signals were just confirmed or contradicted.
    """
    sql = """
        SELECT so.item_id, so.outcome, so.original_z, so.followup_z,
               so.horizon_days, so.checked_at, i.title, i.url, i.source
        FROM signal_outcomes so
        JOIN items i ON i.id = so.item_id
        WHERE so.outcome != 'pending'
          AND so.checked_at >= datetime('now', ?)
        ORDER BY so.checked_at DESC, so.item_id DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, (f"-{hours} hours",)).fetchall()


def get_outcomes(item_ids: list[int]) -> dict[int, sqlite3.Row]:
    """Return outcome rows keyed by item_id (7-day horizon, most recent check)."""
    if not item_ids:
        return {}
    placeholders = ",".join("?" for _ in item_ids)
    sql = f"""
        SELECT item_id, outcome, original_z, followup_z, magnitude
        FROM signal_outcomes
        WHERE item_id IN ({placeholders})
          AND horizon_days = 7
        ORDER BY checked_at DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, tuple(item_ids)).fetchall()
    seen: set[int] = set()
    result: dict[int, sqlite3.Row] = {}
    for row in rows:
        iid = row["item_id"]
        if iid not in seen:
            result[iid] = row
            seen.add(iid)
    return result


def items_ready_for_summary(
    limit: int | None = 75,
    source: str | None = None,
    per_source_cap: int | None = None,
    max_age_days: int | None = None,
) -> list[sqlite3.Row]:
    """Items that passed triage but haven't been summarized yet.

    When per_source_cap is set (and source filter is not), uses a SQLite window
    function (ROW_NUMBER OVER PARTITION BY source) so no single source can claim
    more than per_source_cap slots out of the overall limit.

    Args:
        limit: total max rows returned.
        source: optional source filter; when set, per_source_cap is ignored.
        per_source_cap: max items from any one source (ignored when source is set).
        max_age_days: skip items ingested more than this many days ago, so
            capped-out sources age out instead of accumulating a backlog.
    """
    params: list = []
    age_clause = ""
    if max_age_days is not None:
        age_clause = " AND ingested_at >= datetime('now', ?)"

    if source is not None or per_source_cap is None:
        # Simple path: single-source filter or no per-source cap needed.
        sql = """
            SELECT id, source, source_id, url, title, author, content,
                   published_at, metadata_json, topic, triage_score
            FROM items
            WHERE triage_decision = 'keep'
              AND summary IS NULL
        """
        if max_age_days is not None:
            sql += age_clause
            params.append(f"-{max_age_days} days")
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY triage_score DESC, ingested_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
    else:
        # Window-function path: cap each source independently, then take top-N overall.
        sql = f"""
            SELECT id, source, source_id, url, title, author, content,
                   published_at, metadata_json, topic, triage_score
            FROM (
                SELECT id, source, source_id, url, title, author, content,
                       published_at, ingested_at, metadata_json, topic, triage_score,
                       ROW_NUMBER() OVER (
                           PARTITION BY source
                           ORDER BY triage_score DESC, ingested_at DESC
                       ) AS rn
                FROM items
                WHERE triage_decision = 'keep'
                  AND summary IS NULL{age_clause}
            )
            WHERE rn <= ?
            ORDER BY triage_score DESC, ingested_at DESC
        """
        if max_age_days is not None:
            params.append(f"-{max_age_days} days")
        params.append(per_source_cap)
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

    with get_conn() as conn:
        return conn.execute(sql, tuple(params)).fetchall()


def auto_keep_clipped() -> int:
    """Mark every untriaged clipped item as kept with score=1.0.

    Clips reach this state by virtue of the user's act of clipping — they've
    already self-triaged. We bypass Qwen and shove them straight to the
    summarizer. Returns the number of rows updated.
    """
    sql = """
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = 1.0,
            triaged_at      = datetime('now')
        WHERE source = 'clipped'
          AND triage_decision IS NULL
    """
    with get_conn() as conn:
        cur = conn.execute(sql)
        return cur.rowcount or 0


# Quantitative ingestors pre-filter to anomalous readings only — every item
# that reaches the DB has already passed a z-score or dollar threshold.
# Letting Qwen re-gate them with prose-oriented criteria drops valid signals.
QUANT_SOURCES = ("fred", "cboe", "cftc", "yahoo", "insider", "ftd")


def auto_keep_quantitative() -> int:
    """Auto-keep untriaged items from quantitative ingestors.

    Applies topic_hint from metadata_json directly as the topic so items
    land in the right section of the daily note without Qwen guessing.
    Returns the number of rows updated.
    """
    placeholders = ",".join("?" * len(QUANT_SOURCES))
    sql = f"""
        UPDATE items
        SET triage_decision = 'keep',
            triage_score    = 0.85,
            topic           = COALESCE(
                                json_extract(metadata_json, '$.topic_hint'),
                                'other'
                              ),
            triaged_at      = datetime('now')
        WHERE source IN ({placeholders})
          AND triage_decision IS NULL
    """
    with get_conn() as conn:
        cur = conn.execute(sql, QUANT_SOURCES)
        return cur.rowcount or 0


def _source_pair(conn: sqlite3.Connection, item_id: int) -> tuple[str, str] | None:
    """(source, source_id) for an item — the sink's item_hash derivation key."""
    row = conn.execute(
        "SELECT source, source_id FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    return (row["source"], row["source_id"]) if row else None


def update_triage(
    item_id: int,
    decision: str,        # 'keep' or 'drop'
    score: float,
    topic: str | None,
) -> None:
    """Record a triage outcome on an item."""
    with get_conn() as conn:
        pair = _source_pair(conn, item_id)
        conn.execute(
            """
            UPDATE items
            SET triage_decision = ?,
                triage_score    = ?,
                topic           = ?,
                triaged_at      = datetime('now')
            WHERE id = ?
            """,
            (decision, score, topic, item_id),
        )
    if pair:
        sink.write_triage(pair[0], pair[1], {
            "decision": decision,
            "score":    score,
            "topic":    topic,
        })


def update_summary(
    item_id: int,
    topic: str,
    summary: str,
    why_it_matters: str,
    confidence: str,
    see_also: list[str] | None,
) -> None:
    """Record summarizer output on an item."""
    see_also_json = json.dumps(see_also or [])
    with get_conn() as conn:
        pair = _source_pair(conn, item_id)
        conn.execute(
            """
            UPDATE items
            SET topic          = ?,
                summary        = ?,
                why_it_matters = ?,
                confidence     = ?,
                see_also       = ?,
                summarized_at  = datetime('now')
            WHERE id = ?
            """,
            (topic, summary, why_it_matters, confidence, see_also_json, item_id),
        )
    if pair:
        sink.write_summary(pair[0], pair[1], {
            "summary":        summary,
            "why_it_matters": why_it_matters,
            "see_also":       see_also_json,
            "confidence":     confidence,
        })


def log_summarizer(
    backend: str,
    item_id: int,
    duration_ms: int,
    input_chars: int,
    output_chars: int,
    status: str,
    error: str | None = None,
) -> None:
    """Append a row to summarizer_log for cost/usage tracking."""
    with get_conn() as conn:
        pair = _source_pair(conn, item_id)
        conn.execute(
            """
            INSERT INTO summarizer_log
                (backend, item_id, duration_ms, input_chars, output_chars, status, error)
            VALUES
                (?, ?, ?, ?, ?, ?, ?)
            """,
            (backend, item_id, duration_ms, input_chars, output_chars, status, error),
        )
    # Bronze telemetry — stage='summarize'. Source comes from the item.
    ended = datetime.now(timezone.utc)
    started = ended - timedelta(milliseconds=duration_ms)
    sink.write_telemetry({
        "run_id":       f"{started.isoformat(timespec='seconds')}-summarize-{item_id}",
        "stage":        "summarize",
        "source":       pair[0] if pair else None,
        "started_at":   started.isoformat(timespec="seconds"),
        "ended_at":     ended.isoformat(timespec="seconds"),
        "duration_ms":  duration_ms,
        "items_in":     input_chars,
        "items_out":    output_chars,
        "errors":       0 if status == "ok" else 1,
        "error_detail": error,
        "model_id":     backend,
    })


def triage_stats() -> dict[str, int]:
    """Counts grouped by triage_decision (incl. NULL = pending)."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(triage_decision, 'pending') AS decision,
                   COUNT(*) AS n
            FROM items
            GROUP BY decision
            ORDER BY n DESC
            """
        ).fetchall()
    return {row["decision"]: row["n"] for row in rows}


def summarizer_stats(days: int = 7) -> dict[str, int]:
    """Recent summarizer activity by backend (for cost/budget tracking)."""
    sql = """
        SELECT backend, COUNT(*) AS n,
               SUM(input_chars)  AS in_chars,
               SUM(output_chars) AS out_chars
        FROM summarizer_log
        WHERE run_at >= datetime('now', ?)
        GROUP BY backend
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (f"-{days} days",)).fetchall()
    return {row["backend"]: dict(row) for row in rows}


# ── Phase 3 helpers (Obsidian publishing) ──────────────────────────────


def items_for_publish(date_iso: str) -> dict[str, list[sqlite3.Row]]:
    """Return everything to publish for a given calendar date (YYYY-MM-DD).

    Returns two lists:
      - 'summarized': triage=keep AND summary IS NOT NULL, ordered by topic + score
      - 'kept_unsummarized': triage=keep AND summary IS NULL (cap-overflow leftovers)

    Filters by ingested_at::date = date_iso to align with the daily note's date.
    """
    base = """
        SELECT id, source, source_id, url, title, author, content,
               published_at, ingested_at, metadata_json,
               topic, summary, why_it_matters, confidence, see_also,
               triage_score
        FROM items
        WHERE date(ingested_at) = date(?)
          AND triage_decision = 'keep'
    """
    with get_conn() as conn:
        summarized = conn.execute(
            base + " AND summary IS NOT NULL ORDER BY topic ASC, triage_score DESC",
            (date_iso,),
        ).fetchall()
        kept_unsum = conn.execute(
            base + " AND summary IS NULL ORDER BY triage_score DESC",
            (date_iso,),
        ).fetchall()
    return {"summarized": summarized, "kept_unsummarized": kept_unsum}


def items_by_topic(topic: str) -> list[sqlite3.Row]:
    """All summarized items for a topic, newest first. Used by topic archive writer."""
    sql = """
        SELECT id, source, url, title, author,
               published_at, ingested_at,
               summary, why_it_matters, confidence, see_also, triage_score
        FROM items
        WHERE topic = ?
          AND summary IS NOT NULL
        ORDER BY ingested_at DESC, id DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, (topic,)).fetchall()


def topics_with_summaries() -> list[str]:
    """Distinct topics that have at least one summarized item."""
    sql = """
        SELECT DISTINCT topic
        FROM items
        WHERE topic IS NOT NULL AND summary IS NOT NULL
        ORDER BY topic
    """
    with get_conn() as conn:
        return [row["topic"] for row in conn.execute(sql).fetchall()]


def prune_past_events(days_grace: int = 1) -> int:
    """Delete calendar events whose date has passed (with a grace period). Returns count."""
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM upcoming_events WHERE event_date < date('now', ?)",
            (f"-{days_grace} days",),
        )
        return cur.rowcount or 0


def items_for_week(monday_iso: str, sunday_iso: str) -> list[sqlite3.Row]:
    """Summarized items ingested during a Mon–Sun week, sorted by triage score desc."""
    sql = """
        SELECT id, source, url, title, author,
               published_at, ingested_at, topic,
               summary, why_it_matters, confidence, see_also,
               triage_score, metadata_json, sentiment_label, entities_json
        FROM items
        WHERE triage_decision = 'keep'
          AND summary IS NOT NULL
          AND date(ingested_at) BETWEEN date(?) AND date(?)
        ORDER BY triage_score DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, (monday_iso, sunday_iso)).fetchall()


def upsert_connections(date_iso: str, threads_json: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daily_connections (date, threads_json) VALUES (?, ?)",
            (date_iso, threads_json),
        )


def get_connections(date_iso: str) -> list:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT threads_json FROM daily_connections WHERE date = ?",
            (date_iso,),
        ).fetchone()
    if not row:
        return []
    try:
        return json.loads(row["threads_json"]) or []
    except (json.JSONDecodeError, KeyError):
        return []


def get_fred_signals_window(days: int = 45) -> list[sqlite3.Row]:
    """Latest z-score per FRED series from the past N days.

    Uses a window function to return only the most-recent reading per series,
    so the macro regime classifier always sees current values.
    """
    sql = """
        WITH ranked AS (
            SELECT
                json_extract(metadata_json, '$.series_id') AS series_id,
                CAST(json_extract(metadata_json, '$.z_score') AS REAL) AS z_score,
                ROW_NUMBER() OVER (
                    PARTITION BY json_extract(metadata_json, '$.series_id')
                    ORDER BY ingested_at DESC
                ) AS rn
            FROM items
            WHERE source = 'fred'
              AND ingested_at >= datetime('now', ?)
              AND json_extract(metadata_json, '$.series_id') IS NOT NULL
        )
        SELECT series_id, z_score FROM ranked WHERE rn = 1 ORDER BY series_id
    """
    with get_conn() as conn:
        return conn.execute(sql, (f"-{days} days",)).fetchall()


def upsert_regime(week_iso: str, regime: str, signals_json: str, narrative: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO macro_regime (week, regime, signals_json, narrative)
               VALUES (?, ?, ?, ?)""",
            (week_iso, regime, signals_json, narrative),
        )


def items_for_essay(start_iso: str, end_iso: str, limit: int = 40) -> list[sqlite3.Row]:
    """Top-scored kept items in a date range, returning raw content for the essay agent.

    Reads the content field (original source material), not AI-generated summaries,
    so the essay writer works from primary sources regardless of summarization status.
    """
    sql = """
        SELECT id, source, title, author, url, content,
               published_at, ingested_at, topic, triage_score, metadata_json
        FROM items
        WHERE triage_decision = 'keep'
          AND date(ingested_at) BETWEEN date(?) AND date(?)
          AND content IS NOT NULL
          AND content != ''
        ORDER BY triage_score DESC, ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (start_iso, end_iso, limit)).fetchall()


def connections_for_range(start_iso: str, end_iso: str) -> list[dict]:
    """All daily connection threads in a date range, newest first."""
    sql = """
        SELECT date, threads_json FROM daily_connections
        WHERE date BETWEEN ? AND ?
        ORDER BY date DESC
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (start_iso, end_iso)).fetchall()
    result: list[dict] = []
    for row in rows:
        try:
            threads = json.loads(row["threads_json"]) or []
            result.extend(threads)
        except (json.JSONDecodeError, KeyError):
            pass
    return result


def get_latest_regime() -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT week, regime, signals_json, narrative FROM macro_regime ORDER BY week DESC LIMIT 1"
        ).fetchone()


# ── Feature helpers ────────────────────────────────────────────────────


def items_needing_sentiment(limit: int = 200) -> list[sqlite3.Row]:
    sql = """
        SELECT id, title, summary, why_it_matters
        FROM items
        WHERE triage_decision = 'keep'
          AND summary IS NOT NULL
          AND sentiment_label IS NULL
        ORDER BY triage_score DESC, ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (limit,)).fetchall()


def update_sentiment(item_id: int, label: str, score: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE items SET sentiment_label = ?, sentiment_score = ? WHERE id = ?",
            (label, score, item_id),
        )


def items_needing_entities(limit: int = 500) -> list[sqlite3.Row]:
    sql = """
        SELECT id, title, summary, why_it_matters
        FROM items
        WHERE triage_decision = 'keep'
          AND entities_json IS NULL
        ORDER BY triage_score DESC, ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (limit,)).fetchall()


def update_entities(item_id: int, entities_json: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE items SET entities_json = ? WHERE id = ?",
            (entities_json, item_id),
        )


def upsert_events(events: list[dict]) -> None:
    sql = """
        INSERT OR IGNORE INTO upcoming_events
            (event_type, event_date, title, symbol, metadata_json)
        VALUES
            (:event_type, :event_date, :title, :symbol, :metadata_json)
    """
    with get_conn() as conn:
        for ev in events:
            conn.execute(sql, ev)


def get_upcoming_events(days_ahead: int = 90) -> list[sqlite3.Row]:
    sql = """
        SELECT event_type, event_date, title, symbol, metadata_json
        FROM upcoming_events
        WHERE event_date >= date('now')
          AND event_date <= date('now', ?)
        ORDER BY event_date ASC
    """
    with get_conn() as conn:
        return conn.execute(sql, (f"+{days_ahead} days",)).fetchall()


def top_items_for_cluster(
    cluster_id: str, start_iso: str, end_iso: str, limit: int = 3
) -> list[sqlite3.Row]:
    """Return top-scored items for a cluster within a date range."""
    sql = """
        SELECT id, title, source, published_at, ingested_at, triage_score, url
        FROM items
        WHERE triage_decision = 'keep'
          AND cluster_id = ?
          AND date(ingested_at) BETWEEN date(?) AND date(?)
        ORDER BY triage_score DESC, ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (cluster_id, start_iso, end_iso, limit)).fetchall()


def cluster_counts_for_range(start_iso: str, end_iso: str) -> dict[str, int]:
    sql = """
        SELECT cluster_id, COUNT(*) AS n
        FROM items
        WHERE triage_decision = 'keep'
          AND cluster_id IS NOT NULL
          AND date(ingested_at) BETWEEN date(?) AND date(?)
        GROUP BY cluster_id
    """
    with get_conn() as conn:
        rows = conn.execute(sql, (start_iso, end_iso)).fetchall()
    return {row["cluster_id"]: row["n"] for row in rows}


def get_fred_values_window(days: int = 90) -> list[sqlite3.Row]:
    """FRED latest_value readings per series per day for correlation analysis."""
    sql = """
        SELECT date(ingested_at) AS day,
               json_extract(metadata_json, '$.series_id') AS series_id,
               CAST(json_extract(metadata_json, '$.z_score') AS REAL) AS z_score
        FROM items
        WHERE source = 'fred'
          AND triage_decision = 'keep'
          AND ingested_at >= datetime('now', ?)
          AND json_extract(metadata_json, '$.z_score') IS NOT NULL
        ORDER BY day ASC
    """
    with get_conn() as conn:
        return conn.execute(sql, (f"-{days} days",)).fetchall()


def get_yahoo_pct_window(days: int = 90) -> list[sqlite3.Row]:
    """Yahoo daily pct_change readings per ticker for correlation analysis."""
    sql = """
        WITH ranked AS (
            SELECT date(ingested_at) AS day,
                   json_extract(metadata_json, '$.ticker') AS ticker,
                   CAST(json_extract(metadata_json, '$.pct_change') AS REAL) AS pct_change,
                   ROW_NUMBER() OVER (
                       PARTITION BY date(ingested_at), json_extract(metadata_json, '$.ticker')
                       ORDER BY ingested_at DESC
                   ) AS rn
            FROM items
            WHERE source = 'yahoo'
              AND triage_decision = 'keep'
              AND ingested_at >= datetime('now', ?)
              AND json_extract(metadata_json, '$.pct_change') IS NOT NULL
        )
        SELECT day, ticker, pct_change FROM ranked WHERE rn = 1 ORDER BY day ASC
    """
    with get_conn() as conn:
        return conn.execute(sql, (f"-{days} days",)).fetchall()


def mark_published(item_ids: list[int]) -> None:
    """Stamp obsidian_written_at on items so we know they've been written.

    Note: this is informational only. The writer is idempotent and uses
    file-level state for de-duplication, not this column.
    """
    if not item_ids:
        return
    placeholders = ",".join("?" for _ in item_ids)
    sql = f"""
        UPDATE items
        SET obsidian_written_at = datetime('now')
        WHERE id IN ({placeholders})
    """
    with get_conn() as conn:
        conn.execute(sql, tuple(item_ids))


# ── Storylines (Wave 2: multi-day narrative threading) ─────────────────


def get_storylines(
    statuses: tuple[str, ...] = ("active",), limit: int | None = None
) -> list[sqlite3.Row]:
    """Storylines in the given statuses, most recently moved first."""
    placeholders = ",".join("?" for _ in statuses)
    sql = f"""
        SELECT id, slug, name, status, state, resolution, created_at, last_moved
        FROM storylines
        WHERE status IN ({placeholders})
        ORDER BY last_moved DESC, id DESC
    """
    params: tuple = tuple(statuses)
    if limit:
        sql += " LIMIT ?"
        params += (limit,)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def get_storyline(slug: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, slug, name, status, state, resolution, created_at, last_moved "
            "FROM storylines WHERE slug = ?",
            (slug,),
        ).fetchone()


def create_storyline(slug: str, name: str, state: str, date_iso: str) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO storylines (slug, name, state, status, last_moved) "
            "VALUES (?, ?, ?, 'active', ?)",
            (slug, name, state, date_iso),
        )
        return cur.lastrowid


def move_storyline(slug: str, state: str, date_iso: str) -> None:
    """Update the running state and last_moved; movement reactivates dormant lines."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE storylines SET state = ?, last_moved = ?, status = 'active' "
            "WHERE slug = ?",
            (state, date_iso, slug),
        )


def resolve_storyline(slug: str, resolution: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE storylines SET status = 'resolved', resolution = ? WHERE slug = ?",
            (resolution, slug),
        )


def mark_stale_storylines_dormant(days: int = 14) -> int:
    """Active storylines that haven't moved in N days go dormant. Returns count."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE storylines SET status = 'dormant' "
            "WHERE status = 'active' AND (last_moved IS NULL OR last_moved < date('now', ?))",
            (f"-{days} days",),
        )
        return cur.rowcount or 0


def upsert_storyline_delta(
    storyline_id: int, date_iso: str, delta: str, item_ids: list[int]
) -> None:
    """One delta per storyline per date; re-runs on the same day replace it."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO storyline_deltas (storyline_id, date, delta, item_ids) "
            "VALUES (?, ?, ?, ?)",
            (storyline_id, date_iso, delta, json.dumps(item_ids)),
        )


def get_storyline_deltas(storyline_id: int, limit: int | None = None) -> list[sqlite3.Row]:
    """Timeline for one storyline, newest first."""
    sql = (
        "SELECT date, delta, item_ids FROM storyline_deltas "
        "WHERE storyline_id = ? ORDER BY date DESC"
    )
    params: tuple = (storyline_id,)
    if limit:
        sql += " LIMIT ?"
        params += (limit,)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def storylines_moved_on(date_iso: str) -> list[sqlite3.Row]:
    """Storylines with a delta on the given date (for the Brief), newest first."""
    sql = """
        SELECT s.slug, s.name, s.status, s.state, d.delta, d.item_ids
        FROM storyline_deltas d
        JOIN storylines s ON s.id = d.storyline_id
        WHERE d.date = ?
        ORDER BY d.id ASC
    """
    with get_conn() as conn:
        return conn.execute(sql, (date_iso,)).fetchall()


# ── Topic state-of-play + cluster names (Wave 4) ───────────────────────


def upsert_topic_state(
    topic: str, state: str, changed: str, watch: str, week: str
) -> None:
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO topic_state (topic, state, changed, watch, week, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(topic) DO UPDATE SET
                   state = excluded.state, changed = excluded.changed,
                   watch = excluded.watch, week = excluded.week,
                   updated_at = excluded.updated_at""",
            (topic, state, changed, watch, week),
        )


def get_topic_state(topic: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT topic, state, changed, watch, week, updated_at "
            "FROM topic_state WHERE topic = ?",
            (topic,),
        ).fetchone()


def get_cluster_names() -> dict[str, str]:
    """All cached cluster display names keyed by raw TF-IDF cluster_id."""
    with get_conn() as conn:
        rows = conn.execute("SELECT cluster_id, name FROM cluster_names").fetchall()
    return {row["cluster_id"]: row["name"] for row in rows}


def upsert_cluster_names(mapping: dict[str, str]) -> None:
    with get_conn() as conn:
        for cluster_id, name in mapping.items():
            conn.execute(
                "INSERT OR REPLACE INTO cluster_names (cluster_id, name) VALUES (?, ?)",
                (cluster_id, name),
            )


# ── Predictions (Wave 3: scorecard for essay/debate/weekly calls) ──────

_PREDICTION_COLS = (
    "id, source, source_ref, made_on, due_on, claim, observable, direction, "
    "status, rationale, evidence_ids, resolved_on"
)


def insert_predictions(preds: list[dict]) -> int:
    """Insert extracted predictions; (source, source_ref, claim) dupes are
    ignored so re-extraction over the same document is idempotent. Returns
    the number actually inserted."""
    sql = """
        INSERT OR IGNORE INTO predictions
            (source, source_ref, made_on, due_on, claim, observable, direction)
        VALUES
            (:source, :source_ref, :made_on, :due_on, :claim, :observable, :direction)
    """
    inserted = 0
    with get_conn() as conn:
        for p in preds:
            cur = conn.execute(sql, p)
            inserted += cur.rowcount or 0
    return inserted


def open_predictions(due_by: str | None = None) -> list[sqlite3.Row]:
    """Open predictions, optionally only those due on/before a date. Oldest due first."""
    sql = f"SELECT {_PREDICTION_COLS} FROM predictions WHERE status = 'open'"
    params: tuple = ()
    if due_by:
        sql += " AND due_on <= ?"
        params = (due_by,)
    sql += " ORDER BY due_on ASC, id ASC"
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def resolve_prediction(
    pred_id: int, status: str, rationale: str, evidence_ids: list[int], resolved_on: str
) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE predictions SET status = ?, rationale = ?, evidence_ids = ?, "
            "resolved_on = ? WHERE id = ?",
            (status, rationale, json.dumps(evidence_ids), resolved_on, pred_id),
        )


def predictions_resolved_since(hours: int = 36) -> list[sqlite3.Row]:
    """Predictions resolved in the last N hours (for the Brief scoreboard)."""
    sql = f"""
        SELECT {_PREDICTION_COLS} FROM predictions
        WHERE status != 'open'
          AND resolved_on >= date('now', ?)
        ORDER BY resolved_on DESC, id DESC
    """
    days = max(1, round(hours / 24))
    with get_conn() as conn:
        return conn.execute(sql, (f"-{days} days",)).fetchall()


def predictions_resolved_between(start_iso: str, end_iso: str) -> list[sqlite3.Row]:
    """Predictions resolved within [start, end] (weekly retro)."""
    sql = f"""
        SELECT {_PREDICTION_COLS} FROM predictions
        WHERE status != 'open'
          AND resolved_on BETWEEN ? AND ?
        ORDER BY resolved_on DESC, id DESC
    """
    with get_conn() as conn:
        return conn.execute(sql, (start_iso, end_iso)).fetchall()


def all_predictions(limit: int = 200) -> list[sqlite3.Row]:
    """All predictions for the scorecard: open first (soonest due), then resolved (newest)."""
    sql = f"""
        SELECT {_PREDICTION_COLS} FROM predictions
        ORDER BY CASE WHEN status = 'open' THEN 0 ELSE 1 END,
                 CASE WHEN status = 'open' THEN due_on ELSE '' END ASC,
                 resolved_on DESC, id DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (limit,)).fetchall()


def prediction_stats() -> dict[str, dict[str, int]]:
    """Counts by source and status, e.g. {'essay': {'correct': 3, 'open': 2}}."""
    sql = "SELECT source, status, COUNT(*) AS n FROM predictions GROUP BY source, status"
    out: dict[str, dict[str, int]] = {}
    with get_conn() as conn:
        for row in conn.execute(sql).fetchall():
            out.setdefault(row["source"], {})[row["status"]] = row["n"]
    return out
