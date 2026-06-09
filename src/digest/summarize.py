"""Phase 2 — Summarize. Backend-abstracted, with a kill-switch.

Backends share a common interface: take an item dict, return a SummaryOutput.
Default backend is mlx_local (Qwen3.5-27B on the shared MLX server) — it won
the May 2026 trial vs claude_cli_pro on success rate (92% vs 55%; Pro rate
limits collided with interactive use). Flip SUMMARIZER_BACKEND in .env to
switch — no code change.

Backends:
  - mlx_local:         MLX-LM server, Qwen3.5-27B. $0, fully local. Default.
  - claude_cli_pro:    invokes `claude -p` headless on Sonnet. $0 (subscription),
                       but fails when Pro rate limits are exhausted.
  - haiku_api:         direct Anthropic API, Haiku 4.5 + caching. ~$0.50-1/mo.
  - gemini_flash_free: Google AI Studio free tier. $0.
  - local_qwen:        Ollama Qwen 14B. $0, lower polish on prose.

For all backends, summarizer_log records duration + char counts so you can
see actual usage vs. expectations in `digest stats --summarizer`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from digest_core.summarize.backends import BACKENDS, BackendConfig, BackendError
from digest_core.summarize.runner import extract_json

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)


# ── Output schema ──────────────────────────────────────────────────────


@dataclass
class SummaryOutput:
    topic: str                       # canonical topic from triage taxonomy
    summary: str                     # 2-3 sentences
    why_it_matters: str              # 1-2 sentences, user-specific framing
    confidence: str                  # "low" | "medium" | "high"
    see_also: list[str] = field(default_factory=list)


# ── Prompt construction ────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a personal research analyst. The reader is a senior data/AI leader in financial services with deep interest in: Fed policy & markets, China macro/geopolitics, AI thinkers (Karpathy, Mollick, Willison, Weng, Lambert, Dwarkesh), AI capex by hyperscalers, AI business applications, AI semis, and data-viz inspiration.

For each item, you produce a JSON object with five fields:

1. "topic": one of [fed_markets, china, ai_thinkers, ai_capex, ai_business_apps, ai_semis, data_viz, other]
2. "summary": 2-3 sentences. State the actual content — what was reported, claimed, or shown. No filler like "this article discusses..."
3. "why_it_matters": 1-2 sentences. Frame the implication FOR THIS READER given their interests. Be specific. Bad: "this is important for AI." Good: "Suggests hyperscaler 2026 capex guides may surprise to the upside, with implications for NVDA Q3 expectations."
4. "confidence": "low" | "medium" | "high" — how reliable is this signal? High = primary source (filing, FRED print, named expert). Medium = reputable secondary reporting. Low = social-media speculation, anonymous posts, single-source claims.
5. "see_also": a list of 0-3 short phrases describing topics or events from the user's domain that this item connects to. Examples: "2s10s spread inversion", "Microsoft Q1 capex guide", "Karpathy Software 3.0 thesis". Empty list is acceptable.

Respond with ONLY a single JSON object — no preamble, no markdown fences, no commentary."""


USER_TEMPLATE = """Source: {source}
Title: {title}
Author: {author}
Published: {published}
URL: {url}
Pre-assigned topic from triage: {topic_hint}

Content:
{content}

JSON only:"""


def _build_user_prompt(item: dict[str, Any]) -> str:
    content = (item.get("content") or "").strip()
    # Allow more context than triage since this is the premium step
    if len(content) > 6000:
        content = content[:6000] + "…[truncated]"
    if not content:
        content = "(no body content; reason from title and metadata only)"

    return USER_TEMPLATE.format(
        source=item.get("source", "?"),
        title=item.get("title", "?"),
        author=item.get("author") or "(unknown)",
        published=(item.get("published_at") or "")[:19],
        url=item.get("url") or "(no URL)",
        topic_hint=item.get("topic") or "(none)",
        content=content,
    )


# ── JSON parsing ───────────────────────────────────────────────────────
# extract_json now lives in digest_core.summarize.runner (a brace-depth scan,
# shared with triage + PC) and is imported above.


def _normalize_summary(parsed: dict[str, Any], fallback_topic: str) -> SummaryOutput:
    valid_topics = {
        "fed_markets", "china", "ai_thinkers", "ai_capex",
        "ai_business_apps", "ai_semis", "data_viz", "other",
    }
    valid_confidence = {"low", "medium", "high"}

    topic = str(parsed.get("topic", fallback_topic)).lower().strip()
    if topic not in valid_topics:
        topic = fallback_topic if fallback_topic in valid_topics else "other"

    confidence = str(parsed.get("confidence", "medium")).lower().strip()
    if confidence not in valid_confidence:
        confidence = "medium"

    see_also_raw = parsed.get("see_also") or []
    if isinstance(see_also_raw, str):
        see_also_raw = [see_also_raw]
    see_also = [str(s).strip() for s in see_also_raw if str(s).strip()][:3]

    return SummaryOutput(
        topic=topic,
        summary=str(parsed.get("summary", "")).strip(),
        why_it_matters=str(parsed.get("why_it_matters", "")).strip(),
        confidence=confidence,
        see_also=see_also,
    )


# ── Backends ──────────────────────────────────────────────────────────
# The five transports (claude_cli_pro / haiku_api / gemini_flash_free /
# local_qwen / mlx_local) + BackendError + the BACKENDS registry now live in
# digest_core.summarize.backends, shared with PC. They take
# (system_prompt, user_prompt, BackendConfig); macro injects its system prompt
# and a config built from its settings here.


def _backend_config() -> BackendConfig:
    """Build the shared core BackendConfig from macro settings.

    haiku/gemini model names fall through to the core defaults; max_tokens=600
    preserves macro's historical output cap.
    """
    return BackendConfig(
        timeout_sec=settings.summarizer_timeout_sec,
        max_tokens=600,
        claude_model=settings.summarizer_model,
        anthropic_api_key=settings.anthropic_api_key,
        gemini_api_key=settings.gemini_api_key,
        ollama_host=settings.ollama_host,
        ollama_model=settings.ollama_model,
        mlx_server_url=settings.mlx_server_url,
        mlx_model=settings.mlx_model,
    )


# ── Public API ─────────────────────────────────────────────────────────


def summarize_item(item: dict[str, Any], regime_framing: str = "") -> SummaryOutput:
    """Summarize one item using the configured backend. Raises BackendError on failure."""
    backend_name = settings.summarizer_backend
    backend_fn = BACKENDS.get(backend_name)
    if not backend_fn:
        raise BackendError(
            f"Unknown SUMMARIZER_BACKEND: {backend_name!r}. "
            f"Valid: {sorted(BACKENDS.keys())}"
        )

    user_prompt = _build_user_prompt(item)
    if regime_framing:
        user_prompt = f"[Macro regime: {regime_framing}]\n\n{user_prompt}"
    raw = backend_fn(SYSTEM_PROMPT, user_prompt, _backend_config())
    parsed = extract_json(raw)
    if not parsed:
        raise BackendError(
            f"Backend {backend_name} returned unparseable output: {raw[:300]!r}"
        )
    return _normalize_summary(parsed, fallback_topic=item.get("topic") or "other")


def run_summarize(
    limit: int | None = None,
    source: str | None = None,
    uncapped: bool = False,
) -> dict[str, int]:
    """Summarize items that passed triage. Returns counts.

    Args:
        limit: explicit max rows; defaults to SUMMARIZER_MAX_PER_RUN.
        source: optional source filter (e.g. "clipped" to summarize only clips).
        uncapped: if True, ignore SUMMARIZER_MAX_PER_RUN entirely. Used for the
            clipped pass — clipped items are user-curated and should never be
            dropped just because the cap was hit.
    """
    if uncapped:
        cap: int | None = None
        per_source_cap: int | None = None
    elif limit is not None:
        cap = limit
        per_source_cap = None  # explicit limit overrides per-source cap
    else:
        cap = settings.summarizer_max_per_run
        per_source_cap = settings.summarizer_max_per_source if source is None else None
    # Age-out: items that kept losing the cap fight stop being eligible after
    # SUMMARIZER_MAX_AGE_DAYS instead of piling up forever. Clipped items are
    # user-curated and exempt; 0 disables the age-out entirely.
    max_age_days = settings.summarizer_max_age_days or None
    if source == "clipped":
        max_age_days = None
    rows = db.items_ready_for_summary(
        limit=cap, source=source, per_source_cap=per_source_cap, max_age_days=max_age_days
    )
    if not rows:
        logger.info("summarize: nothing ready (source=%s)", source or "all")
        return {"ready": 0, "succeeded": 0, "failed": 0}

    backend = settings.summarizer_backend

    # Pre-flight: verify MLX server can actually generate (not just respond to HTTP).
    # Uses a tiny 1-token inference with a 20s timeout to detect hung generation threads.
    if backend == "mlx_local":
        probe_url = settings.mlx_server_url.rstrip("/") + "/v1/chat/completions"
        try:
            probe = requests.post(probe_url, json={
                "model": settings.mlx_model,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 1,
                "chat_template_kwargs": {"enable_thinking": False},
            }, timeout=20)
            probe.raise_for_status()
        except requests.ConnectionError:
            logger.error(
                "summarize: MLX server not reachable at %s — start it first, skipping batch",
                settings.mlx_server_url,
            )
            return {"ready": len(rows), "succeeded": 0, "failed": 0}
        except (requests.Timeout, Exception) as exc:
            logger.error(
                "summarize: MLX server health-check failed (%s) — "
                "server may have crashed, skipping batch. "
                "Restart with: mlx_lm.server --model mlx-community/Qwen3.5-27B-4bit --port 8080",
                exc,
            )
            return {"ready": len(rows), "succeeded": 0, "failed": 0}

    # Fetch regime framing once for this batch (single DB lookup, not per-item)
    regime_framing = ""
    try:
        from digest.macro_regime import get_current_framing
        regime_framing = get_current_framing()
        if regime_framing:
            logger.info("summarize: applying macro regime framing")
    except Exception:
        pass

    counts = {"ready": len(rows), "succeeded": 0, "failed": 0}
    for row in rows:
        item = dict(row)
        item_id = item["id"]
        t0 = time.perf_counter()
        input_chars = len(item.get("content") or "")
        status = "ok"
        error_msg: str | None = None
        output_chars = 0

        try:
            output = summarize_item(item, regime_framing=regime_framing)
            db.update_summary(
                item_id=item_id,
                topic=output.topic,
                summary=output.summary,
                why_it_matters=output.why_it_matters,
                confidence=output.confidence,
                see_also=output.see_also,
            )
            output_chars = len(output.summary) + len(output.why_it_matters)
            counts["succeeded"] += 1
            logger.info(
                "summarize: id=%d topic=%s confidence=%s (%.1fs)",
                item_id, output.topic, output.confidence,
                time.perf_counter() - t0,
            )
        except BackendError as exc:
            status = "error"
            error_msg = str(exc)[:500]
            counts["failed"] += 1
            logger.error("summarize: id=%d failed: %s", item_id, error_msg)
        except Exception as exc:  # noqa: BLE001
            status = "error"
            error_msg = f"{type(exc).__name__}: {exc}"[:500]
            counts["failed"] += 1
            logger.exception("summarize: id=%d crashed", item_id)
        finally:
            db.log_summarizer(
                backend=backend,
                item_id=item_id,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                input_chars=input_chars,
                output_chars=output_chars,
                status=status,
                error=error_msg,
            )

    return counts
