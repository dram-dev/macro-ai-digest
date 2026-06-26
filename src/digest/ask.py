"""Ask-the-archive — local RAG over the kept-item corpus.

Embeds every kept+summarized item once (cached in item_embeddings), then for a
question: embeds the query, takes the top-k by cosine, and synthesizes an
answer with [n] citations through the configured summarizer backend (MLX-local
by default). Reuses digest.embeddings (Ollama) for vectors.

Degrades gracefully: if the embedding backend is down you get a clear error; if
the LLM step fails you still get the retrieved sources back, unsynthesized.
"""
from __future__ import annotations

import logging

from digest_core.summarize.backends import BACKENDS, BackendConfig, BackendError

from digest import db, embeddings
from digest.config import settings

logger = logging.getLogger(__name__)

_EMBED_BATCH = 256
_DEFAULT_K = 8


class AskError(RuntimeError):
    """Raised when the question can't be answered (no corpus / embeddings down)."""


def ensure_embeddings() -> int:
    """Embed and cache any kept+summarized items missing a vector. Returns count.

    First call on a fresh corpus embeds everything (~30s for a few thousand
    items); later calls only touch newly-summarized items.
    """
    import numpy as np

    model = settings.embed_model
    missing = db.items_missing_embeddings(model)
    if not missing:
        return 0

    embedded = 0
    for start in range(0, len(missing), _EMBED_BATCH):
        batch = missing[start : start + _EMBED_BATCH]
        texts = [f"{r['title'] or ''} {r['summary'] or ''}".strip() for r in batch]
        vecs = embeddings.embed_texts(texts)
        if vecs is None:
            logger.warning("ask: embedding backend unavailable — cached %d so far", embedded)
            break
        store = [
            (batch[i]["id"], len(v), np.asarray(v, dtype="float32").tobytes())
            for i, v in enumerate(vecs)
        ]
        db.store_embeddings(model, store)
        embedded += len(store)
    return embedded


def retrieve(question: str, k: int = _DEFAULT_K, days: int = 0) -> list[dict]:
    """Return the top-k most similar items to `question` (with a `score` each)."""
    import numpy as np

    ensure_embeddings()
    rows = db.load_embeddings(settings.embed_model, days=days)
    if not rows:
        raise AskError(
            "No embedded items to search. Summarize some items first, and make "
            f"sure the embedding model is available (`ollama pull {settings.embed_model}`)."
        )

    ids = [r["item_id"] for r in rows]
    mat = np.stack([np.frombuffer(r["vector"], dtype="float32") for r in rows])
    mat = mat / np.clip(np.linalg.norm(mat, axis=1, keepdims=True), 1e-9, None)

    qv = embeddings.embed_texts([question])
    if not qv:
        raise AskError(
            "Could not embed the question — is Ollama running and "
            f"'{settings.embed_model}' pulled?"
        )
    q = np.asarray(qv[0], dtype="float32")
    q = q / max(float(np.linalg.norm(q)), 1e-9)

    sims = mat @ q
    top = sims.argsort()[-k:][::-1]
    top_ids = [ids[i] for i in top]
    meta = db.items_by_ids(top_ids)

    out: list[dict] = []
    for rank, i in enumerate(top):
        row = meta.get(ids[i])
        if row is None:
            continue
        d = dict(row)
        d["score"] = float(sims[i])
        out.append(d)
    return out


_SYSTEM = (
    "You are a research analyst answering a question using ONLY the provided "
    "digest items. Cite the items you use inline as [n], matching the numbered "
    "context. Be concise and specific — lead with numbers, names, and dates. If "
    "the items don't contain the answer, say so plainly rather than guessing."
)


def _context_block(rows: list[dict]) -> str:
    lines = []
    for n, r in enumerate(rows, 1):
        date = (r.get("published_at") or "")[:10]
        head = f"[{n}] ({r.get('source', '?')} · {date}) {r.get('title', '')}".strip()
        body = " ".join(
            s for s in (r.get("summary"), r.get("why_it_matters")) if s
        )
        lines.append(f"{head}\n{body}".strip())
    return "\n\n".join(lines)


def _synthesize(question: str, rows: list[dict]) -> str | None:
    """Run the configured backend to answer from context. None on failure."""
    backend_fn = BACKENDS.get(settings.summarizer_backend)
    if not backend_fn:
        logger.error("ask: unknown SUMMARIZER_BACKEND %r", settings.summarizer_backend)
        return None
    user = (
        f"Question: {question}\n\nContext items:\n{_context_block(rows)}\n\n"
        "Answer (with [n] citations):"
    )
    cfg = BackendConfig(
        timeout_sec=settings.summarizer_timeout_sec,
        max_tokens=800,
        claude_model=settings.summarizer_model,
        anthropic_api_key=settings.anthropic_api_key,
        gemini_api_key=settings.gemini_api_key,
        ollama_host=settings.ollama_host,
        ollama_model=settings.ollama_model,
        mlx_server_url=settings.mlx_server_url,
        mlx_model=settings.mlx_model,
    )
    try:
        return backend_fn(_SYSTEM, user, cfg).strip() or None
    except (BackendError, Exception) as exc:  # noqa: BLE001
        logger.warning("ask: synthesis failed (%s) — returning sources only", exc)
        return None


def answer_question(question: str, k: int = _DEFAULT_K, days: int = 0) -> dict:
    """Retrieve + synthesize. Returns {'answer': str|None, 'sources': list[dict]}.

    `answer` is None when the LLM step fails; `sources` is still populated so the
    caller can show the retrieved items.
    """
    sources = retrieve(question, k=k, days=days)
    answer = _synthesize(question, sources) if sources else None
    return {"answer": answer, "sources": sources}
