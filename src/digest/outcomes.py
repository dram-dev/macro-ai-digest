"""Signal outcome tracker (Idea 2).

Checks FRED/CBOE/CFTC z-score signals N days after ingestion to see whether
the anomaly was confirmed, contradicted, or reverted to neutral. Uses only
data already in the DB — no external API calls.

Outcome classification (7-day default horizon):
  confirmed    — follow-up z same sign AND |z| ≥ 50% of original magnitude
  contradicted — follow-up z opposite sign AND |follow-up z| ≥ 1.0
  neutral      — signal faded (magnitude decay below either threshold)
  pending      — no newer reading available yet in DB

Results stored in signal_outcomes and surfaced in the Signal leaderboard.
"""
from __future__ import annotations

import json
import logging

from digest import db

logger = logging.getLogger(__name__)

_SOURCE_KEY: dict[str, str] = {
    "fred": "series_id",
    "cboe": "symbol",
    "cftc": "contract",
}

_DEFAULT_HORIZON = 7


def _classify(original_z: float, followup_z: float) -> tuple[str, float]:
    magnitude = abs(followup_z - original_z)
    same_sign = (original_z >= 0) == (followup_z >= 0)
    if same_sign and abs(followup_z) >= abs(original_z) * 0.5:
        return "confirmed", magnitude
    if not same_sign and abs(followup_z) >= 1.0:
        return "contradicted", magnitude
    return "neutral", magnitude


def run_outcomes(horizon_days: int = _DEFAULT_HORIZON, limit: int = 500) -> dict[str, int]:
    """Check outcomes for eligible quant signals. Returns counts by outcome."""
    items = db.items_for_outcome_check(horizon_days=horizon_days, limit=limit)
    if not items:
        logger.info("outcomes: nothing to check")
        return {"checked": 0, "confirmed": 0, "contradicted": 0, "neutral": 0, "pending": 0}

    counts: dict[str, int] = {
        "checked": 0, "confirmed": 0, "contradicted": 0, "neutral": 0, "pending": 0,
    }

    for row in items:
        item_id  = row["id"]
        source   = row["source"]
        ingested = row["ingested_at"]

        meta_key = _SOURCE_KEY.get(source)
        if not meta_key:
            continue

        try:
            meta = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            continue

        key_value  = meta.get(meta_key)
        original_z = meta.get("z_score")

        if not key_value or original_z is None:
            continue

        try:
            original_z = float(original_z)
        except (TypeError, ValueError):
            continue

        followup_z = db.get_followup_z(source, meta_key, str(key_value), ingested)

        if followup_z is None:
            outcome   = "pending"
            magnitude = None
        else:
            outcome, magnitude = _classify(original_z, followup_z)

        db.upsert_outcome(
            item_id=item_id,
            horizon_days=horizon_days,
            outcome=outcome,
            original_z=original_z,
            followup_z=followup_z,
            magnitude=magnitude,
        )
        counts["checked"] += 1
        counts[outcome]   += 1
        logger.debug("outcomes: item=%d %s → %s", item_id, source, outcome)

    logger.info(
        "outcomes: checked=%d confirmed=%d contradicted=%d neutral=%d pending=%d",
        counts["checked"], counts["confirmed"], counts["contradicted"],
        counts["neutral"], counts["pending"],
    )
    return counts
