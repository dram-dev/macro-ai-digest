"""Macro regime classifier — weekly FRED signal aggregation.

Each weekly run classifies the macro environment into one of five regimes
using FRED z-scores stored in items.metadata_json. The result is persisted
to the macro_regime table and flows into three places:

  1. Weekly note header — an Obsidian callout block with label + narrative
  2. Weekly synthesis prompt — regime framing prepended for Claude context
  3. Summarizer system prompt — regime-aware 'why it matters' framing

Regime taxonomy:
  tightening    — Inflation elevated, Fed holding above neutral
  on_hold       — Fed paused, data-dependent, mixed signals
  easing_start  — Fed cutting, inflation near target, growth slowing
  recession_risk — Inverted curve + credit stress + weakening growth
  soft_landing  — Inflation at target, employment solid, curve normalizing
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date

from digest import db

logger = logging.getLogger(__name__)

# ── Taxonomy ───────────────────────────────────────────────────────────

REGIME_LABELS: dict[str, str] = {
    "tightening":     "Tightening",
    "on_hold":        "On Hold",
    "easing_start":   "Easing — Early Cycle",
    "recession_risk": "Recession Risk",
    "soft_landing":   "Soft Landing",
}

# Injected into the summarizer system prompt when a regime is active.
REGIME_FRAMING: dict[str, str] = {
    "tightening":
        "The macro regime is Tightening: inflation above target, Fed holding above neutral. "
        "Emphasize rate sensitivity, terminal-rate uncertainty, and sectors exposed to "
        "higher-for-longer rates (duration, leveraged credit, rate-sensitive tech capex).",
    "on_hold":
        "The macro regime is On Hold: Fed paused, watching incoming data. "
        "Emphasize any signal that shifts the next-move calculus — CPI surprises, "
        "labor market softening, or early signs of credit stress.",
    "easing_start":
        "The macro regime is Easing — Early Cycle: Fed cutting, inflation nearing target. "
        "Emphasize rate-sensitive beneficiaries (duration, growth equities, EM credit), "
        "capex re-acceleration timelines, and AI spend as a leading indicator.",
    "recession_risk":
        "The macro regime signals Recession Risk: inverted curve, widening spreads, "
        "weakening growth indicators. Emphasize defensive positioning, credit quality "
        "bifurcation, earnings guidance risk, and which AI capex commitments are "
        "truly cycle-proof.",
    "soft_landing":
        "The macro regime is Soft Landing: inflation near target, employment solid, "
        "curve normalizing. Emphasize durability of growth, AI capex cycle momentum, "
        "and whether current valuations already price the benign outcome.",
}

# ── Signal mapping ─────────────────────────────────────────────────────
# FRED series → (dimension, signed weight)
# Positive weight: higher z → more of this dimension (e.g. higher CPI z = more inflation)
# Negative weight: higher z → less (e.g. higher unemployment z = weaker growth)

_SIGNAL_MAP: dict[str, tuple[str, float]] = {
    # Inflation
    "PCEPILFE":     ("inflation", +1.2),
    "CPIAUCSL":     ("inflation", +1.0),
    "CPILFESL":     ("inflation", +0.8),
    "T10YIE":       ("inflation", +0.6),
    "T5YIFR":       ("inflation", +0.4),
    # Yield curve (positive = steeper / normalizing; negative = inverted / stressed)
    "T10Y2Y":       ("curve",    +1.5),
    "T10Y3M":       ("curve",    +1.5),
    # Credit conditions (higher spread = tighter / stressed conditions)
    "BAMLH0A0HYM2": ("credit",   +1.2),
    "BAMLC0A0CM":   ("credit",   +0.8),
    # Growth / labor
    "PAYEMS":       ("growth",   +1.2),
    "UNRATE":       ("growth",   -1.0),
    "ICSA":         ("growth",   -0.8),
}

_SERIES_LABELS: dict[str, str] = {
    "PCEPILFE":     "Core PCE",
    "CPIAUCSL":     "CPI",
    "CPILFESL":     "Core CPI",
    "T10YIE":       "10Y breakeven",
    "T5YIFR":       "5Y5Y forward inflation",
    "T10Y2Y":       "2s10s spread",
    "T10Y3M":       "10Y-3M spread",
    "BAMLH0A0HYM2": "HY OAS",
    "BAMLC0A0CM":   "IG OAS",
    "PAYEMS":       "Payrolls",
    "UNRATE":       "Unemployment",
    "ICSA":         "Jobless claims",
}


# ── Result dataclass ───────────────────────────────────────────────────

@dataclass
class RegimeResult:
    regime: str
    label: str
    dimensions: dict[str, float]
    top_signals: list[tuple[str, float]] = field(default_factory=list)
    narrative: str = ""
    framing: str = ""


# ── Classification logic ───────────────────────────────────────────────

def _weighted_mean(values: list[tuple[float, float]]) -> float:
    total_w = sum(w for _, w in values)
    if not total_w:
        return 0.0
    return sum(v * w for v, w in values) / total_w


def _classify(dims: dict[str, float]) -> str:
    inflation = dims.get("inflation", 0.0)
    curve = dims.get("curve", 0.0)
    credit = dims.get("credit", 0.0)
    growth = dims.get("growth", 0.0)

    # Recession risk: inverted curve + credit stress + weak growth
    if curve < -0.8 and credit > 0.8 and growth < -0.3:
        return "recession_risk"

    # Soft landing: curve normalizing, credit benign, growth ok, inflation falling
    if curve > -0.3 and credit < 0.3 and growth > 0.1 and inflation < -0.2:
        return "soft_landing"

    # Active tightening: clearly elevated inflation
    if inflation > 0.7:
        return "tightening"

    # Early easing: inflation falling, growth slowing
    if inflation < -0.3 and growth < -0.1:
        return "easing_start"

    return "on_hold"


def _build_narrative(
    regime: str,
    dims: dict[str, float],
    top_signals: list[tuple[str, float]],
) -> str:
    label = REGIME_LABELS[regime]
    signal_desc = "; ".join(f"{s} z={z:+.1f}" for s, z in top_signals[:3])
    dim_parts = [
        f"{k}={v:+.2f}"
        for k, v in sorted(dims.items(), key=lambda x: abs(x[1]), reverse=True)
        if abs(v) > 0.05
    ]
    dim_desc = ", ".join(dim_parts) if dim_parts else "insufficient FRED data"
    signal_part = f"Key signals: {signal_desc}." if signal_desc else "Sparse FRED data this window."
    return f"**{label}** — {signal_part} Composite scores: {dim_desc}."


# ── Public API ─────────────────────────────────────────────────────────

def compute_regime(lookback_days: int = 45) -> RegimeResult:
    """Classify macro regime from FRED z-scores in the DB.

    Queries the most recent z-score per series within the lookback window,
    builds weighted composite dimension scores, applies classification rules,
    and persists the result to the macro_regime table.

    Falls back gracefully to 'on_hold' when FRED data is sparse.
    """
    rows = db.get_fred_signals_window(days=lookback_days)

    # Deduplicate to most-recent reading per series (query already does this via ROW_NUMBER)
    signal_z: dict[str, float] = {}
    for row in rows:
        sid = row["series_id"]
        z = row["z_score"]
        if sid and z is not None:
            signal_z[sid] = float(z)

    # Compute weighted composite per dimension
    dim_inputs: dict[str, list[tuple[float, float]]] = {}
    for sid, (dim, weight) in _SIGNAL_MAP.items():
        if sid in signal_z:
            # Apply sign: positive weight means z directly contributes; negative inverts it
            contribution = signal_z[sid] * (1.0 if weight > 0 else -1.0)
            dim_inputs.setdefault(dim, []).append((contribution, abs(weight)))

    dims = {d: _weighted_mean(vals) for d, vals in dim_inputs.items()}
    regime = _classify(dims)

    # Top signals by absolute z-score for the narrative
    top_signals = sorted(
        [
            (_SERIES_LABELS.get(sid, sid), signal_z[sid])
            for sid in _SIGNAL_MAP
            if sid in signal_z
        ],
        key=lambda x: abs(x[1]),
        reverse=True,
    )[:5]

    narrative = _build_narrative(regime, dims, top_signals)
    framing = REGIME_FRAMING[regime]

    logger.info(
        "macro_regime: %s | inflation=%.2f curve=%.2f credit=%.2f growth=%.2f | %d signals",
        regime,
        dims.get("inflation", 0.0), dims.get("curve", 0.0),
        dims.get("credit", 0.0), dims.get("growth", 0.0),
        len(signal_z),
    )

    week_iso = date.today().strftime("%G-W%V")
    db.upsert_regime(
        week_iso=week_iso,
        regime=regime,
        signals_json=json.dumps({"dims": dims, "top_signals": top_signals}),
        narrative=narrative,
    )

    return RegimeResult(
        regime=regime,
        label=REGIME_LABELS[regime],
        dimensions=dims,
        top_signals=top_signals,
        narrative=narrative,
        framing=framing,
    )


def get_current_framing() -> str:
    """Return regime framing for the summarizer, empty string if unavailable."""
    try:
        row = db.get_latest_regime()
        if row:
            return REGIME_FRAMING.get(row["regime"], "")
    except Exception:
        pass
    return ""
