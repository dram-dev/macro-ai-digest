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
    """Items ingested recently with no triage decision yet."""
    sql = """
        SELECT id, source, source_id, url, title, author, content,
               published_at, metadata_json
        FROM items
        WHERE triage_decision IS NULL
        ORDER BY ingested_at DESC
        LIMIT ?
    """
    with get_conn() as conn:
        return conn.execute(sql, (limit,)).fetchall()


def items_ready_for_summary(
    limit: int | None = 20,
    source: str | None = None,
) -> list[sqlite3.Row]:
    """Items that passed triage but haven't been summarized yet.

    Ordered by triage_score DESC so the top-N most-relevant are picked
    when more items pass than the cap allows.

    Args:
        limit: max rows; pass ``None`` for unlimited (used by the clipped pass).
        source: optional source filter (e.g. "clipped").
    """
    sql = """
        SELECT id, source, source_id, url, title, author, content,
               published_at, metadata_json, topic, triage_score
        FROM items
        WHERE triage_decision = 'keep'
          AND summary IS NULL
    """
    params: list = []
    if source is not None:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY triage_score DESC, ingested_at DESC"
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
