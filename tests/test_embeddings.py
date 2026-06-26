"""Embeddings backend — batch contract + graceful failure. No network."""
from __future__ import annotations

from digest import embeddings


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_empty_input_returns_empty_list(monkeypatch):
    # Should not even hit the network.
    monkeypatch.setattr(
        embeddings.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    assert embeddings.embed_texts([]) == []


def test_embed_texts_returns_vectors(monkeypatch):
    payload = {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}
    monkeypatch.setattr(embeddings.requests, "post", lambda *a, **k: _Resp(payload))
    assert embeddings.embed_texts(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]


def test_network_failure_returns_none(monkeypatch):
    def _boom(*a, **k):
        raise OSError("ollama down")

    monkeypatch.setattr(embeddings.requests, "post", _boom)
    assert embeddings.embed_texts(["a"]) is None


def test_shape_mismatch_returns_none(monkeypatch):
    payload = {"embeddings": [[0.1, 0.2]]}  # 1 vector for 2 texts
    monkeypatch.setattr(embeddings.requests, "post", lambda *a, **k: _Resp(payload))
    assert embeddings.embed_texts(["a", "b"]) is None


def test_embeddings_available_probe(monkeypatch):
    monkeypatch.setattr(embeddings.requests, "post", lambda *a, **k: _Resp({"embeddings": [[1.0]]}))
    assert embeddings.embeddings_available() is True
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts: None)
    assert embeddings.embeddings_available() is False
