"""TF-IDF clustering for narrative thread detection (Idea 1).

Clusters all kept+summarized items using TF-IDF + KMeans so the Signal
leaderboard can surface which items belong to the same emerging story.

Each cluster is labelled by its top 3 TF-IDF bigram/unigram terms, stored as:
  cluster_id = "nvidia, inference, datacenter"

Items sharing a cluster_id badge in the Signal prose callout belong to the
same narrative thread and should be read together.
"""
from __future__ import annotations

import logging

from digest import db

logger = logging.getLogger(__name__)

_N_CLUSTERS  = 8
_MAX_FEATURES = 500
_MIN_DF      = 2   # term must appear in ≥ 2 documents
_MIN_ITEMS   = 10  # need at least this many items to cluster meaningfully


def run_clustering() -> dict[str, int]:
    """Cluster all kept+summarized items by narrative thread. Returns counts."""
    try:
        from sklearn.cluster import KMeans
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:
        logger.error("cluster: scikit-learn not installed — run `uv add scikit-learn`")
        return {"items": 0, "clusters": 0}

    rows = db.items_for_clustering()
    if len(rows) < _MIN_ITEMS:
        logger.info("cluster: only %d items — skipping (need ≥ %d)", len(rows), _MIN_ITEMS)
        return {"items": len(rows), "clusters": 0}

    ids   = [r["id"] for r in rows]
    texts = [
        f"{r['title'] or ''} {r['summary'] or ''}".strip()
        for r in rows
    ]

    vectorizer = TfidfVectorizer(
        max_features=_MAX_FEATURES,
        min_df=_MIN_DF,
        stop_words="english",
        ngram_range=(1, 2),
    )
    try:
        X = vectorizer.fit_transform(texts)
    except ValueError as exc:
        logger.warning("cluster: vectorizer failed: %s", exc)
        return {"items": len(rows), "clusters": 0}

    n_clusters = min(_N_CLUSTERS, len(rows))
    km = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    km.fit(X)

    # Label each cluster with its top 3 TF-IDF terms
    feature_names = vectorizer.get_feature_names_out()
    cluster_labels: list[str] = []
    for center in km.cluster_centers_:
        top_idx   = center.argsort()[-3:][::-1]
        top_terms = ", ".join(feature_names[i] for i in top_idx)
        cluster_labels.append(top_terms)

    db.update_cluster_ids({
        item_id: cluster_labels[label]
        for item_id, label in zip(ids, km.labels_)
    })

    logger.info("cluster: %d items → %d clusters", len(rows), n_clusters)
    return {"items": len(rows), "clusters": n_clusters}
