"""Forward-to-file capture — URL/X/text handling + clipped-ingestor round-trip."""
from __future__ import annotations

import pytest

from digest import capture
from digest.ingest.clipped import ClippedIngestor, _split_frontmatter


@pytest.fixture
def clip_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(capture.settings, "obsidian_clip_dir", str(tmp_path))
    return tmp_path


def test_first_url():
    assert capture.first_url("see https://x.com/a/status/1 now") == "https://x.com/a/status/1"
    assert capture.first_url("no link here") is None


def test_capture_text_only(clip_dir):
    res = capture.capture("Just a thought worth keeping")
    assert res["kind"] == "text"
    assert res["path"].exists()
    fm, body = _split_frontmatter(res["path"].read_text())
    assert body.strip() == "Just a thought worth keeping"
    assert "clipping" in fm["tags"]


def test_capture_article_uses_fulltext(clip_dir, monkeypatch):
    monkeypatch.setattr(capture, "fetch_fulltext", lambda url: "FULL ARTICLE BODY")
    res = capture.capture("https://example.com/story")
    assert res["kind"] == "article"
    _, body = _split_frontmatter(res["path"].read_text())
    assert "FULL ARTICLE BODY" in body


def test_capture_tweet_uses_fxtwitter(clip_dir, monkeypatch):
    monkeypatch.setattr(
        capture, "fetch_tweet",
        lambda url: {"text": "Nvidia just guided capex way up", "author": "@analyst"},
    )
    res = capture.capture("https://x.com/analyst/status/123")
    assert res["kind"] == "tweet"
    fm, body = _split_frontmatter(res["path"].read_text())
    assert "Nvidia just guided capex" in body
    assert fm["author"] == "@analyst"


def test_capture_roundtrips_through_clipped_ingestor(clip_dir, monkeypatch):
    """A captured file must be readable by the real clipped ingestor."""
    monkeypatch.setattr(capture, "fetch_fulltext", lambda url: "Body from the web")
    capture.capture("https://example.com/x", title="My Capture")

    items = ClippedIngestor(clip_dir=clip_dir).fetch()
    assert len(items) == 1
    item = items[0]
    assert item.source == "clipped"
    assert item.title == "My Capture"
    assert item.url == "https://example.com/x"
    assert "Body from the web" in item.content


def test_fetch_tweet_parses_fxtwitter_json(monkeypatch):
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"tweet": {"text": "hello world",
                              "author": {"screen_name": "jdoe", "name": "J Doe"}}}

    monkeypatch.setattr(capture.requests, "get", lambda *a, **k: _Resp())
    out = capture.fetch_tweet("https://x.com/jdoe/status/99")
    assert out == {"text": "hello world", "author": "@jdoe"}


def test_fetch_tweet_ignores_non_x_urls(monkeypatch):
    monkeypatch.setattr(
        capture.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not fetch")),
    )
    assert capture.fetch_tweet("https://example.com/not-a-tweet") is None
