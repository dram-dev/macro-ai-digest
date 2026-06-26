"""RAG ask-the-archive — embedding cache, retrieval ranking, answer assembly."""
from __future__ import annotations

import pytest

from digest import ask, db, embeddings


def _vec(texts):
    """Content-based 2-D vectors: nvidia → x-axis, everything else → y-axis."""
    return [[1.0, 0.0] if "nvidia" in t.lower() else [0.0, 1.0] for t in texts]


def _seed():
    with db.get_conn() as conn:
        for i, t in enumerate([
            "Nvidia GPU demand surges", "Nvidia datacenter ramp",
            "Fed holds rates steady", "Fed minutes hawkish",
        ]):
            conn.execute(
                "INSERT INTO items (source, source_id, title, content, triage_decision, summary) "
                "VALUES ('rss', ?, ?, 'c', 'keep', ?)", (f"id{i}", t, t),
            )


def test_ensure_embeddings_caches_and_is_incremental(fresh_db, monkeypatch):
    _seed()
    monkeypatch.setattr(embeddings, "embed_texts", _vec)
    assert ask.ensure_embeddings() == 4   # first pass embeds all
    assert ask.ensure_embeddings() == 0   # nothing missing now


def test_retrieve_ranks_by_similarity(fresh_db, monkeypatch):
    _seed()
    monkeypatch.setattr(embeddings, "embed_texts", _vec)
    top = ask.retrieve("nvidia gpu roadmap", k=2)
    assert len(top) == 2
    assert all("Nvidia" in s["title"] for s in top)   # nvidia items win
    assert top[0]["score"] >= top[1]["score"]


def test_answer_question_assembles_answer_and_sources(fresh_db, monkeypatch):
    _seed()
    monkeypatch.setattr(embeddings, "embed_texts", _vec)
    monkeypatch.setattr(ask, "_synthesize", lambda q, rows: "Demand is hot [1].")
    res = ask.answer_question("nvidia", k=2)
    assert res["answer"] == "Demand is hot [1]."
    assert len(res["sources"]) == 2


def test_answer_question_returns_sources_when_synthesis_fails(fresh_db, monkeypatch):
    _seed()
    monkeypatch.setattr(embeddings, "embed_texts", _vec)
    monkeypatch.setattr(ask, "_synthesize", lambda q, rows: None)
    res = ask.answer_question("nvidia", k=2)
    assert res["answer"] is None
    assert res["sources"]   # still populated


def test_retrieve_raises_on_empty_corpus(fresh_db, monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", _vec)
    with pytest.raises(ask.AskError):
        ask.retrieve("anything", k=3)
