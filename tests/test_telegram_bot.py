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


def test_capture_replies_with_inline_takeaway(sent, monkeypatch):
    from digest.summarize import SummaryOutput

    monkeypatch.setattr(
        tb.capture, "capture",
        lambda text, **k: {"kind": "article", "chars": 1500, "title": "Capex news",
                           "url": "https://x", "body": "MSFT raised capex guidance."},
    )
    monkeypatch.setattr(
        "digest.summarize.summarize_item",
        lambda item, **k: SummaryOutput(
            topic="ai_capex", summary="MSFT guided capex up.",
            why_it_matters="Bullish for NVDA.", confidence="high"),
    )
    assert tb._handle_message({"text": "https://x"}) is True
    msg = sent[-1]
    assert "Takeaway" in msg
    assert "MSFT guided capex up." in msg
    assert "Bullish for NVDA." in msg


def test_capture_takeaway_skipped_when_no_body(monkeypatch):
    # No body → no summarizer call, no takeaway, capture still succeeds.
    monkeypatch.setattr(
        "digest.summarize.summarize_item",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not summarize")),
    )
    assert tb._capture_takeaway({"title": "T"}) is None


def test_forward_author_extraction():
    assert tb._forward_author({"forward_origin": {"sender_user": {"first_name": "Ada"}}}) == "Ada"
    assert tb._forward_author({"forward_sender_name": "Hidden User"}) == "Hidden User"
    assert tb._forward_author({}) is None


# ── Gmail sign-in from the phone ──────────────────────────────────────────────

def test_gmail_command_sends_googles_link(sent, monkeypatch):
    from digest.ingest import gmail
    monkeypatch.setattr(gmail, "begin_phone_signin",
                        lambda: "https://accounts.google.com/o/oauth2/auth?client_id=c&state=s&code_challenge=x")
    assert tb._handle_message({"text": "/gmail"}) is True
    assert 'href="https://accounts.google.com/o/oauth2/auth?client_id=c&amp;state=s' in sent[-1]
    assert "Copy that page's whole address" in sent[-1]


def test_the_pasted_address_finishes_and_is_never_captured(sent, monkeypatch):
    from digest.ingest import gmail
    got = []
    monkeypatch.setattr(gmail, "finish_phone_signin",
                        lambda text: got.append(text) or {"refresh_token": True, "ends_in_days": 7})
    monkeypatch.setattr(tb, "_do_capture", lambda *a, **k: pytest.fail("the code was filed as a clipping"))
    url = "http://localhost:8765/?state=s&code=4/abc&scope=https://www.googleapis.com/auth/gmail.readonly"
    assert tb._handle_message({"text": url}) is True
    assert got == [url]
    assert "Gmail is signed in again" in sent[-1]
    assert "7 days" in sent[-1] and "Publish app" in sent[-1]
    assert tb._handle_message({"text": "/gmail " + url}) is True and got[-1] == url


def test_a_sign_in_problem_is_said_plainly(sent, monkeypatch):
    from digest.ingest import gmail

    def refuse(text):
        raise gmail.SignInError("That address belongs to a <different> sign-in.")
    monkeypatch.setattr(gmail, "finish_phone_signin", refuse)
    assert tb._handle_message({"text": "http://localhost:8765/?state=x&code=y"}) is True
    assert "⚠️" in sent[-1] and "&lt;different&gt;" in sent[-1]


def test_no_refresh_token_is_said(sent, monkeypatch):
    from digest.ingest import gmail
    monkeypatch.setattr(gmail, "finish_phone_signin", lambda text: {"refresh_token": False, "ends_in_days": None})
    assert tb._handle_message({"text": "http://localhost:8765/?state=x&code=y"}) is True
    assert "about an hour" in sent[-1]


def test_an_ordinary_link_is_still_captured(sent, monkeypatch):
    got = []
    monkeypatch.setattr(tb, "_do_capture", lambda text, message: got.append(text) or True)
    assert tb._handle_message({"text": "https://www.ft.com/content/abc?code=1"}) is True
    assert got == ["https://www.ft.com/content/abc?code=1"]
