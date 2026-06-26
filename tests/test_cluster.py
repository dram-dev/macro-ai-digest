"""Clustering — embeddings+HDBSCAN path, TF-IDF fallback, and labelling."""
from __future__ import annotations

import pytest
from digest import cluster, db, embeddings


def _seed(n_nvidia: int = 6, n_fed: int = 6) -> None:
    """Insert kept+summarized items in two clear themes."""
    with db.get_conn() as conn:
        i = 0
        for _ in range(n_nvidia):
            t = f"Nvidia GPU datacenter inference demand surges report {i}"
            conn.execute(
                "INSERT INTO items (source, source_id, title, content, triage_decision, summary) "
                "VALUES ('rss', ?, ?, 'c', 'keep', ?)", (f"id{i}", t, t),
            )
            i += 1
        for _ in range(n_fed):
            t = f"Fed holds rates steady amid inflation data uncertainty note {i}"
            conn.execute(
                "INSERT INTO items (source, source_id, title, content, triage_decision, summary) "
                "VALUES ('rss', ?, ?, 'c', 'keep', ?)", (f"id{i}", t, t),
            )
            i += 1


def _fake_embed(texts):
    """Deterministic, content-based vectors: two separable groups with jitter."""
    out = []
    for j, t in enumerate(texts):
        eps = 0.001 * j
        out.append([1.0 + eps, eps] if "Nvidia" in t else [eps, 1.0 + eps])
    return out


def _cluster_ids():
    with db.get_conn() as conn:
        return {r["title"]: r["cluster_id"] for r in conn.execute("SELECT title, cluster_id FROM items")}


def test_top_terms_extracts_label():
    label = cluster._top_terms(["nvidia gpu demand", "nvidia gpu datacenter", "gpu supply"])
    assert label and "gpu" in label


def test_label_by_cluster_marks_noise_none():
    texts = ["nvidia gpu", "nvidia chips", "fed rates", "fed policy"]
    labels = cluster._label_by_cluster(texts, [0, 0, 1, -1])
    assert labels[0] == labels[1]      # same cluster → same label
    assert labels[2] is not None       # its own cluster
    assert labels[3] is None           # noise stays unclustered


def test_run_clustering_embeddings_path(fresh_db, monkeypatch):
    _seed()
    monkeypatch.setattr(embeddings, "embed_texts", _fake_embed)
    res = cluster.run_clustering()
    assert res["items"] == 12
    assert res["clusters"] == 2

    ids = _cluster_ids()
    nvidia = {cid for t, cid in ids.items() if t.startswith("Nvidia")}
    fed = {cid for t, cid in ids.items() if t.startswith("Fed")}
    assert len(nvidia) == 1 and None not in nvidia      # all nvidia share one cluster
    assert len(fed) == 1 and None not in fed            # all fed share one cluster
    assert nvidia.isdisjoint(fed)                       # and the two differ


@pytest.mark.filterwarnings("ignore:Number of distinct clusters")
def test_run_clustering_falls_back_to_tfidf(fresh_db, monkeypatch):
    _seed()
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts: None)  # backend down
    res = cluster.run_clustering()
    assert res["items"] == 12
    assert res["clusters"] >= 1
    assert any(cid for cid in _cluster_ids().values())  # labels were written


def test_run_clustering_skips_when_too_few(fresh_db):
    _seed(n_nvidia=2, n_fed=2)  # only 4 < _MIN_ITEMS
    res = cluster.run_clustering()
    assert res["clusters"] == 0
