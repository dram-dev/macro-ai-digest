"""Clustering for narrative-thread detection (Idea 1).

Groups kept+summarized items into emerging stories so the Signal leaderboard
and velocity tracker can see which items belong together. Each cluster is
labelled by its top TF-IDF terms, stored as:

    cluster_id = "nvidia, inference, datacenter"

Two strategies, same output contract:
  • embeddings + HDBSCAN (preferred) — semantic grouping via local Ollama
    vectors; finds its own cluster count and drops unrelated items as noise.
  • TF-IDF + KMeans (fallback) — the original lexical approach, used whenever
    embeddings are unavailable (model not pulled / Ollama down).

Either way cluster_id stays a comma-separated term label, so velocity and the
Claude-named `cluster_names` cache keep working unchanged.
"""
from __future__ import annotations

import logging

from digest import db

logger = logging.getLogger(__name__)

_N_CLUSTERS   = 8     # KMeans fallback: fixed cluster count
_MAX_FEATURES = 500
_MIN_DF       = 2     # fallback: term must appear in ≥ 2 documents
_MIN_ITEMS    = 10    # need at least this many items to cluster meaningfully
_LABEL_TERMS  = 3     # top-N terms in a cluster_id label


def run_clustering() -> dict[str, int]:
    """Cluster all kept+summarized items by narrative thread. Returns counts."""
    try:
        import sklearn  # noqa: F401
    except ImportError:
        logger.error("cluster: scikit-learn not installed — run `uv add scikit-learn`")
        return {"items": 0, "clusters": 0}

    rows = db.items_for_clustering()
    if len(rows) < _MIN_ITEMS:
        logger.info("cluster: only %d items — skipping (need ≥ %d)", len(rows), _MIN_ITEMS)
        return {"items": len(rows), "clusters": 0}

    ids   = [r["id"] for r in rows]
    texts = [f"{r['title'] or ''} {r['summary'] or ''}".strip() for r in rows]

    labels = _cluster_embeddings(texts)
    method = "embeddings"
    if labels is None:
        labels = _cluster_tfidf(texts)
        method = "tfidf"
    if labels is None:
        return {"items": len(rows), "clusters": 0}

    # Skip noise items (label None) — they stay unclustered rather than forced
    # into a bucket they don't belong to.
    mapping = {ids[i]: lab for i, lab in enumerate(labels) if lab}
    db.update_cluster_ids(mapping)
    n_clusters = len(set(mapping.values()))
    logger.info("cluster: %d items → %d clusters (%s)", len(rows), n_clusters, method)
    return {"items": len(rows), "clusters": n_clusters}


def _cluster_embeddings(texts: list[str]) -> list[str | None] | None:
    """Semantic clustering. Returns a per-item label list, or None to fall back."""
    from digest.embeddings import embed_texts

    vecs = embed_texts(texts)
    if not vecs:
        return None
    try:
        import numpy as np
        from sklearn.cluster import HDBSCAN
    except ImportError:
        return None

    X = np.asarray(vecs, dtype="float32")
    # Normalize so euclidean distance ranks like cosine similarity.
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    X = X / norms

    from digest.config import settings
    # 'leaf' selection picks fine-grained, homogeneous clusters; the default
    # 'eom' over-merges financial headlines into one 400+ item mega-cluster.
    # Most items legitimately belong to no tight thread → they fall out as
    # noise (-1) and stay unclustered, which is more honest than the old
    # KMeans force-bucketing everything into a fixed 8.
    clusterer = HDBSCAN(
        min_cluster_size=max(2, settings.cluster_min_size),
        metric="euclidean",
        cluster_selection_method="leaf",
        copy=True,
    )
    cluster_idx = clusterer.fit_predict(X)
    if not any(c >= 0 for c in cluster_idx):
        logger.info("cluster: embeddings produced only noise — falling back to TF-IDF")
        return None
    return _label_by_cluster(texts, list(cluster_idx))


def _cluster_tfidf(texts: list[str]) -> list[str | None] | None:
    """Original TF-IDF + KMeans path. Returns a per-item label list or None."""
    from sklearn.cluster import KMeans
    from sklearn.feature_extraction.text import TfidfVectorizer

    vectorizer = TfidfVectorizer(
        max_features=_MAX_FEATURES, min_df=_MIN_DF,
        stop_words="english", ngram_range=(1, 2),
    )
    try:
        X = vectorizer.fit_transform(texts)
    except ValueError as exc:
        logger.warning("cluster: vectorizer failed: %s", exc)
        return None

    n_clusters = min(_N_CLUSTERS, len(texts))
    km = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    km.fit(X)

    feature_names = vectorizer.get_feature_names_out()
    cluster_labels = []
    for center in km.cluster_centers_:
        top_idx = center.argsort()[-_LABEL_TERMS:][::-1]
        cluster_labels.append(", ".join(feature_names[i] for i in top_idx))
    return [cluster_labels[label] for label in km.labels_]


def _label_by_cluster(texts: list[str], cluster_idx: list[int]) -> list[str | None]:
    """Assign each item the top-term label of its cluster (None for noise = -1)."""
    out: list[str | None] = [None] * len(texts)
    for c in sorted({c for c in cluster_idx if c >= 0}):
        members = [i for i, cc in enumerate(cluster_idx) if cc == c]
        label = _top_terms([texts[i] for i in members]) or f"cluster-{c}"
        for i in members:
            out[i] = label
    return out


def _top_terms(texts: list[str], k: int = _LABEL_TERMS) -> str | None:
    """Top-k TF-IDF terms across a set of texts, as a comma-joined label."""
    import numpy as np
    from sklearn.feature_extraction.text import TfidfVectorizer

    try:
        vec = TfidfVectorizer(
            max_features=_MAX_FEATURES, stop_words="english", ngram_range=(1, 2),
        )
        X = vec.fit_transform(texts)
    except ValueError:
        return None
    scores = np.asarray(X.sum(axis=0)).ravel()
    names = vec.get_feature_names_out()
    top = scores.argsort()[-k:][::-1]
    return ", ".join(names[i] for i in top)
