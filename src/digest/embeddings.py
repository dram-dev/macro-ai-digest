"""Local text embeddings via Ollama — for clustering (and later, retrieval).

The MLX server only does completions, so embeddings come from Ollama, the same
local runtime triage already uses. Pull the model once (free, ~270 MB):

    ollama pull nomic-embed-text

Everything degrades gracefully: if the model isn't pulled or the server is
down, `embed_texts` returns None and callers fall back (clustering reverts to
TF-IDF). Nothing here is required for the pipeline to run.
"""
from __future__ import annotations

import logging

import requests

from digest.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 120
_BATCH = 256  # cap per request; one POST for the whole corpus outgrows _TIMEOUT


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Embed texts. Returns one vector per text, or None on failure.

    Chunked so the request size stays bounded as the archive grows: a single
    POST for the whole corpus eventually exceeds the timeout, and because a
    failure here is indistinguishable from "model not pulled", that silently
    downgraded clustering to the TF-IDF fallback with only a log line.

    An empty input returns an empty list (not None) — that's success with
    nothing to do, distinct from a backend failure.
    """
    if not texts:
        return []
    if len(texts) > _BATCH:
        out: list[list[float]] = []
        for start in range(0, len(texts), _BATCH):
            chunk = _embed_chunk(texts[start : start + _BATCH])
            if chunk is None:
                return None
            out.extend(chunk)
        return out
    return _embed_chunk(texts)


def _embed_chunk(texts: list[str]) -> list[list[float]] | None:
    """One request's worth of embeddings."""
    url = f"{settings.ollama_host}/api/embed"
    try:
        r = requests.post(
            url,
            json={"model": settings.embed_model, "input": texts},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "embeddings: request failed (%s) — is '%s' pulled? try `ollama pull %s`",
            exc, settings.embed_model, settings.embed_model,
        )
        return None
    vecs = data.get("embeddings")
    if not vecs or len(vecs) != len(texts):
        logger.warning(
            "embeddings: bad response shape (%d vectors for %d texts)",
            len(vecs or []), len(texts),
        )
        return None
    return vecs


def embeddings_available() -> bool:
    """Cheap probe — can we embed right now? (model pulled + server up)."""
    return embed_texts(["ping"]) is not None
