"""HuggingFace model release tracker.

Polls the HF Hub API for new model releases from tier-1 AI labs and surfaces
each qualifying release as a digest item. A model qualifies if:
  - It was created within the last LOOKBACK_HOURS, AND
  - It has ≥ MIN_LIKES likes, OR its author is in TIER1_ORGS.

HF Hub API is public and unauthenticated for public models.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

HF_API_MODELS = "https://huggingface.co/api/models"
MODEL_PAGE = "https://huggingface.co/{model_id}"

LOOKBACK_HOURS = 36
MIN_LIKES = 200

TIER1_ORGS = {
    "meta-llama",
    "mistralai",
    "google",
    "Qwen",
    "anthropic",
    "microsoft",
    "deepseek-ai",
    "openai",
    "cohere-for-ai",
    "nvidia",
    "EleutherAI",
    "01-ai",
}

# Per-org fetch limit; total items will be filtered to recent + qualifying only
_ORG_FETCH_LIMIT = 10


class HFIngestor(IngestorBase):
    name = "huggingface"

    def fetch(self) -> list[IngestedItem]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
        seen_ids: set[str] = set()
        items: list[IngestedItem] = []

        for org in sorted(TIER1_ORGS):
            try:
                models = self._fetch_org_models(org, cutoff)
                for m in models:
                    model_id = m.get("id") or m.get("modelId", "")
                    if not model_id or model_id in seen_ids:
                        continue
                    seen_ids.add(model_id)

                    likes = m.get("likes", 0) or 0
                    if likes < MIN_LIKES and org not in TIER1_ORGS:
                        continue

                    item = self._to_item(m, model_id, org)
                    if item:
                        items.append(item)
            except Exception as exc:  # noqa: BLE001
                logger.warning("huggingface: failed for org %s: %s", org, exc)

        logger.info("huggingface: %d qualifying model releases in last %dh", len(items), LOOKBACK_HOURS)
        return items

    def _fetch_org_models(self, org: str, cutoff: datetime) -> list[dict[str, Any]]:
        r = requests.get(
            HF_API_MODELS,
            params={
                "author": org,
                "sort": "createdAt",
                "direction": "-1",
                "limit": _ORG_FETCH_LIMIT,
                "full": "true",
            },
            timeout=20,
        )
        r.raise_for_status()
        models = r.json()
        if not isinstance(models, list):
            return []

        recent = []
        for m in models:
            created_at = m.get("createdAt") or m.get("created_at")
            if not created_at:
                continue
            try:
                # HF returns ISO 8601: "2024-01-15T12:34:56.000Z"
                ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts >= cutoff:
                recent.append(m)
        return recent

    def _to_item(self, m: dict[str, Any], model_id: str, org: str) -> IngestedItem | None:
        likes = m.get("likes", 0) or 0
        downloads = m.get("downloads", 0) or 0
        pipeline_tag = m.get("pipeline_tag") or "unknown"
        tags = m.get("tags") or []
        created_at_str = m.get("createdAt") or m.get("created_at", "")

        try:
            published = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            published = None

        # Extract a few model-card lines for context
        card_data = m.get("cardData") or {}
        license_tag = card_data.get("license") or next(
            (t.replace("license:", "") for t in tags if t.startswith("license:")), None
        )

        # Build a compact content summary
        content_parts = [f"Model: {model_id}", f"Task: {pipeline_tag}"]
        if likes:
            content_parts.append(f"Likes: {likes:,}")
        if downloads:
            content_parts.append(f"Downloads (30d): {downloads:,}")
        if license_tag:
            content_parts.append(f"License: {license_tag}")
        notable_tags = [t for t in tags if not t.startswith(("license:", "arxiv:", "region:"))][:8]
        if notable_tags:
            content_parts.append(f"Tags: {', '.join(notable_tags)}")

        # Pull model-card description if available (first 600 chars)
        description = (m.get("description") or "").strip()
        if description:
            content_parts.append(f"\n{description[:600]}")

        return IngestedItem(
            source=self.name,
            source_id=model_id,
            title=f"[HF] {model_id} — {pipeline_tag} ({likes:,} ❤)",
            url=MODEL_PAGE.format(model_id=model_id),
            author=org,
            content="\n".join(content_parts),
            published_at=published,
            metadata={
                "topic_hint": "ai_semis",
                "org": org,
                "likes": likes,
                "downloads": downloads,
                "pipeline_tag": pipeline_tag,
                "license": license_tag,
            },
        )
