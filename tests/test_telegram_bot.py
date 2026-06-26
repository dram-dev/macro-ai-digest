"""Interactive ask-bot — authorization, reply formatting, message handling."""
from __future__ import annotations

import pytest

from digest import ask, telegram_bot as tb


@pytest.fixture
def sent(monkeypatch):
    """Capture outbound messages; stub the typing indicator."""
    msgs: list[str] = []
    monkeypatch.setattr(tb.notifier, "send", lambda text: msgs.append(text) or True)
    monkeypatch.setattr(tb.notifier, "send_chat_action", lambda *a, **k: None)
    return msgs


def test_is_authorized_matches_only_configured_chat(monkeypatch):
    monkeypatch.setattr(tb.settings, "telegram_chat_id", "123")
    assert tb._is_authorized({"message": {"chat": {"id": 123}}}) is True
    assert tb._is_authorized({"message": {"chat": {"id": 999}}}) is False
    assert tb._is_authorized({}) is False  # no message → not authorized


def test_format_reply_renders_answer_and_sources():
    result = {
        "answer": "Capex is rising <fast> & hot",
        "sources": [
            {"title": "MSFT capex", "source": "rss", "published_at": "2026-06-24",
             "url": "https://x.test/a?b=1&c=2"},
        ],
    }
    out = tb._format_reply(result)
    assert "Capex is rising &lt;fast&gt; &amp; hot" in out  # escaped
    assert "[1] MSFT capex" in out
    assert "b=1&amp;c=2" in out


def test_format_reply_handles_missing_synthesis():
    out = tb._format_reply({"answer": None, "sources": [
        {"title": "T", "source": "rss", "published_at": "2026-06-24", "url": None},
    ]})
    assert "synthesis unavailable" in out
    assert "[1] T" in out


def test_handle_message_answers_question(sent, monkeypatch):
    monkeypatch.setattr(
        ask, "answer_question",
        lambda q, **k: {"answer": "A [1]", "sources": [
            {"title": "Item", "source": "rss", "published_at": "2026-06-24", "url": None}]},
    )
    assert tb._handle_message({"text": "what's up with capex?"}) is True
    assert sent and "A [1]" in sent[-1]


def test_handle_message_command_sends_help(sent):
    assert tb._handle_message({"text": "/start"}) is True
    assert "Ask the digest archive" in sent[-1]


def test_handle_message_ignores_empty(sent):
    assert tb._handle_message({"text": "   "}) is False
    assert sent == []


def test_handle_message_reports_ask_error(sent, monkeypatch):
    def _raise(q, **k):
        raise ask.AskError("no corpus")

    monkeypatch.setattr(ask, "answer_question", _raise)
    assert tb._handle_message({"text": "question?"}) is True
    assert "no corpus" in sent[-1]


def test_link_message_routes_to_capture(sent, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        tb.capture, "capture",
        lambda text, **k: captured.update(text=text, author=k.get("author"))
        or {"kind": "article", "chars": 1200, "title": "An article"},
    )
    assert tb._handle_message({"text": "https://example.com/a"}) is True
    assert captured["text"] == "https://example.com/a"
    assert "Captured full article" in sent[-1]


def test_forwarded_message_routes_to_capture(sent, monkeypatch):
    monkeypatch.setattr(
        tb.capture, "capture",
        lambda text, **k: {"kind": "tweet", "chars": 240, "title": "@nicetweet"},
    )
    update = {"text": "some tweet text", "forward_origin": {"sender_user_name": "Jane"}}
    assert tb._handle_message(update) is True
    assert "Captured X post" in sent[-1]


def test_forward_author_extraction():
    assert tb._forward_author({"forward_origin": {"sender_user": {"first_name": "Ada"}}}) == "Ada"
    assert tb._forward_author({"forward_sender_name": "Hidden User"}) == "Hidden User"
    assert tb._forward_author({}) is None
