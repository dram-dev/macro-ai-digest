"""SQLite schema and helpers. Raw sqlite3 — no ORM, keeps things boring."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from digest.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    url             TEXT,
    title           TEXT NOT NULL,
    author          TEXT,
    content         TEXT,
    published_at    TEXT,
    ingested_at     TEXT NOT NULL DEFAULT (datetime('now')),
    metadata_json   TEXT,
    topic           TEXT,
    summary         TEXT,
    why_it_matters  TEXT,
    confidence      TEXT,
    see_also        TEXT,
    triage_score    REAL,
    triage_decision TEXT,
    triaged_at      TEXT,
    summarized_at   TEXT,
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_items_source        ON items(source);
CREATE INDEX IF NOT EXISTS idx_items_published     ON items(published_at);
CREATE INDEX IF NOT EXISTS idx_items_topic         ON items(topic);
CREATE INDEX IF NOT EXISTS idx_items_ingested      ON items(ingested_at);

CREATE TABLE IF NOT EXISTS run_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT NOT NULL DEFAULT (datetime('now')),
    run_type        TEXT NOT NULL,
    source          TEXT NOT NULL,
    items_fetched   INTEGER,
    items_new       INTEGER,
    duration_ms     INTEGER,
    status          TEXT NOT NULL,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_runlog_run_at ON run_log(run_at);

CREATE TABLE IF NOT EXISTS fred_baseline (
    series_id       TEXT PRIMARY KEY,
    mean_delta      REAL,
    stddev_delta    REAL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- For Phase 2 cost/usage tracking on the summarizer step.
CREATE TABLE IF NOT EXISTS summarizer_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT NOT NULL DEFAULT (datetime('now')),
    backend         TEXT NOT NULL,
    item_id         INTEGER NOT NULL,
    duration_ms     INTEGER,
    input_chars     INTEGER,
    output_chars    INTEGER,
    status          TEXT NOT NULL,
    error           TEXT
);

CREATE INDEX IF NOT EXISTS idx_sumlog_run_at ON summarizer_log(run_at);
"""

# Phase 1 → Phase 2 migration. Idempotent.
MIGRATIONS = [
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
]


def init_db(db_path: Path | None = None) -> None:
    """Create DB file and schema if missing. Apply Phase-2 migrations idempotently."""
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
        # Run ALTERs; ignore "duplicate column" errors so it stays idempotent
        for stmt in MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        conn.commit()


@contextmanager
def get_conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager for a DB connection with row factory set."""
    path = db_path or settings.db_path
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_items(items: Iterable["IngestedItem"]) -> int:  # noqa: F821
    """Insert new items, ignore duplicates. Returns count of new rows."""
    sql = """
        INSERT OR IGNORE INTO items
            (source, source_id, url, title, author, content, published_at, metadata_json)
        VALUES
            (:source, :source_id, :url, :title, :author, :content, :published_at, :metadata_json)
    """
    inserted = 0
    with get_conn() as conn:
        for item in items:
            d = asdict(item)
            d["metadata_json"] = json.dumps(d.pop("metadata", {}) or {})
            if isinstance(d.get("published_at"), datetime):
                d["published_at"] = d["published_at"].isoformat()
            cur = conn.execute(sql, d)
            if cur.rowcount:
                inserted += 1
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
    sql = """
        INSERT INTO run_log
            (run_type, source, items_fetched, items_new, duration_ms, status, error)
        VALUES
            (?, ?, ?, ?, ?, ?, ?)
    """
    with get_conn() as conn:
        conn.execute(sql, (run_type, source, items_fetched, items_new, duration_ms, status, error))


def item_stats() -> dict[str, int]:
    """Return item counts grouped by source."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT source, COUNT(*) AS n FROM items GROUP BY source ORDER BY n DESC"
        ).fetchall()
    return {row["source"]: row["n"] for row in rows}


def recent_items(source: str | None = None, limit: int = 20) -> list[sqlite3.Row]:
    """Return most recently ingested items, optionally filtered by source."""
    sql = "SELECT id, source, title, url, published_at, ingested_at FROM items"
    params: tuple = ()
    if source:
        sql += " WHERE source = ?"
        params = (source,)
    sql += " ORDER BY ingested_at DESC LIMIT ?"
    params = (*params, limit)
    with get_conn() as conn:
        return conn.execute(sql, params).fetchall()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
               triage_score, metadata_json
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


def items_ready_for_summary(
    limit: int | None = 75,
    source: str | None = None,
    per_source_cap: int | None = None,
) -> list[sqlite3.Row]:
    """Items that passed triage but haven't been summarized yet.

    When per_source_cap is set (and source filter is not), uses a SQLite window
    function (ROW_NUMBER OVER PARTITION BY source) so no single source can claim
    more than per_source_cap slots out of the overall limit.

    Args:
        limit: total max rows returned.
        source: optional source filter; when set, per_source_cap is ignored.
        per_source_cap: max items from any one source (ignored when source is set).
    """
    params: list = []

    if source is not None or per_source_cap is None:
        # Simple path: single-source filter or no per-source cap needed.
        sql = """
            SELECT id, source, source_id, url, title, author, content,
                   published_at, metadata_json, topic, triage_score
            FROM items
            WHERE triage_decision = 'keep'
              AND summary IS NULL
        """
        if source is not None:
            sql += " AND source = ?"
            params.append(source)
        sql += " ORDER BY triage_score DESC, ingested_at DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
    else:
        # Window-function path: cap each source independently, then take top-N overall.
        sql = """
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
                  AND summary IS NULL
            )
            WHERE rn <= ?
            ORDER BY triage_score DESC, ingested_at DESC
        """
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


def update_triage(
    item_id: int,
    decision: str,        # 'keep' or 'drop'
    score: float,
    topic: str | None,
) -> None:
    """Record a triage outcome on an item."""
    with get_conn() as conn:
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
        conn.execute(
            """
            INSERT INTO summarizer_log
                (backend, item_id, duration_ms, input_chars, output_chars, status, error)
            VALUES
                (?, ?, ?, ?, ?, ?, ?)
            """,
            (backend, item_id, duration_ms, input_chars, output_chars, status, error),
        )


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


def items_for_week(monday_iso: str, sunday_iso: str) -> list[sqlite3.Row]:
    """Summarized items ingested during a Mon–Sun week, sorted by triage score desc."""
    sql = """
        SELECT id, source, url, title, author,
               published_at, ingested_at, topic,
               summary, why_it_matters, confidence, see_also, triage_score
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


def get_latest_regime() -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT week, regime, signals_json, narrative FROM macro_regime ORDER BY week DESC LIMIT 1"
        ).fetchone()


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
