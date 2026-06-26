"""Telegram notify sink — send contract, dedup, threshold, and HTML escaping.

Never touches the network: requests.post is monkeypatched. DB-backed tests use
the `fresh_db` fixture so dedup is exercised against a real notify_log table.
"""
from __future__ import annotations

import pytest

from digest import db
from digest.config import Settings
from digest.sinks import notify


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("bot8814:ABCdef", "8814:ABCdef"),   # doubled 'bot' prefix → stripped
        ("BOT8814:ABCdef", "8814:ABCdef"),   # case-insensitive
        ("8814:ABCdef", "8814:ABCdef"),      # clean token → untouched
        ("  8814:ABCdef  ", "8814:ABCdef"),  # surrounding whitespace trimmed
        ("", ""),                            # empty stays empty (disabled)
    ],
)
def test_token_bot_prefix_is_stripped(raw, expected):
    s = Settings(_env_file=None, TELEGRAM_BOT_TOKEN=raw)
    assert s.telegram_bot_token == expected


class _Resp:
    def raise_for_status(self) -> None:
        pass


@pytest.fixture
def captured(monkeypatch):
    """Capture sent payloads; return the list. Notifier is enabled."""
    sent: list[dict] = []

    def _post(url, json, timeout):  # noqa: A002 - mirrors requests.post kwarg
        sent.append(json)
        return _Resp()

    monkeypatch.setattr(notify.requests, "post", _post)
    monkeypatch.setattr(notify.notifier, "enabled", True)
    monkeypatch.setattr(notify.notifier, "token", "t")
    monkeypatch.setattr(notify.notifier, "chat_id", "c")
    return sent


def test_disabled_notifier_is_noop(monkeypatch):
    monkeypatch.setattr(notify.notifier, "enabled", False)
    # Even if requests.post would blow up, disabled short-circuits before it.
    monkeypatch.setattr(
        notify.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError)
    )
    assert notify.notifier.send("hi") is False


def test_send_posts_html_payload(captured):
    assert notify.notifier.send("<b>hello</b>") is True
    assert captured[0]["parse_mode"] == "HTML"
    assert captured[0]["text"] == "<b>hello</b>"
    assert captured[0]["chat_id"] == "c"


def test_send_swallows_network_error(monkeypatch):
    monkeypatch.setattr(notify.notifier, "enabled", True)

    def _boom(*a, **k):
        raise OSError("down")

    monkeypatch.setattr(notify.requests, "post", _boom)
    assert notify.notifier.send("hi") is False


def test_html_escaping_in_signal():
    row = {
        "id": 1,
        "topic": "AI & capex",
        "title": "Nvidia <beats> & raises",
        "why_it_matters": "margins > expected",
        "triage_score": 0.91,
        "url": "https://x.test/a?b=1&c=2",
    }
    out = notify._format_signal(row)
    assert "AI &amp; capex" in out
    assert "&lt;beats&gt;" in out
    assert "margins &gt; expected" in out
    # the href URL is attribute-escaped too
    assert "b=1&amp;c=2" in out


def test_signal_header_has_no_leading_star():
    row = {"id": 1, "topic": "Fed", "title": "T", "why_it_matters": "w",
           "triage_score": 0.88, "url": "https://x"}
    out = notify._format_signal(row)
    assert out.startswith("<b>Top signal</b> · Fed")  # no redundant leading ⭐
    assert "(⭐ 0.88)" in out                          # score keeps its star


def test_signal_meta_line_fields():
    row = {
        "id": 1, "topic": "AI capex", "title": "MSFT capex", "why_it_matters": "big",
        "triage_score": 0.91, "url": "https://x", "source": "rss",
        "metadata_json": '{"feed": "SemiAnalysis"}',
        "published_at": "2026-06-24T13:00:00", "sentiment_label": "bullish",
    }
    out = notify._format_signal(row, storyline="Hyperscaler capex")
    assert "SemiAnalysis" in out          # feed name preferred over raw source
    assert "2026-06-24" in out            # publication date
    assert "🟢 bullish" in out            # sentiment + emoji
    assert "📖 Hyperscaler capex" in out  # storyline


def test_source_name_resolution():
    assert notify._source_name("rss", '{"feed": "SemiAnalysis"}') == "SemiAnalysis"
    assert notify._source_name("hn", None) == "Hacker News"        # pretty map
    assert notify._source_name("weirdsrc", None) == "Weirdsrc"     # title-case fallback


def test_storyline_names_for_items(fresh_db):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO storylines (id, slug, name, state) VALUES (1, 'hc', 'Hyperscaler capex', 's')"
        )
        conn.execute(
            "INSERT INTO storyline_deltas (storyline_id, date, delta, item_ids) "
            "VALUES (1, '2026-06-24', 'd', '[123, 7]')"
        )
    assert db.storyline_names_for_items([123, 7, 999]) == {
        123: "Hyperscaler capex",
        7: "Hyperscaler capex",
    }
    # substring 12 must NOT false-match inside 123 (Python membership, not LIKE)
    assert db.storyline_names_for_items([12]) == {}


def _seed_signal(source_id: str, score: float, *, title: str = "T") -> None:
    """Insert a kept + summarized item directly so it qualifies as a signal."""
    with db.get_conn() as conn:
        conn.execute(
            """INSERT INTO items (source, source_id, title, url, content,
                                  triage_decision, triage_score, summary,
                                  why_it_matters, topic)
               VALUES ('rss', ?, ?, 'https://x.test/a', 'c',
                       'keep', ?, 'summary', 'why', 'AI')""",
            (source_id, title, score),
        )


def test_top_signals_threshold_and_dedup(fresh_db, captured, monkeypatch):
    monkeypatch.setattr(notify.settings, "notify_min_score", 0.80)
    monkeypatch.setattr(notify.settings, "notify_max_per_run", 5)
    _seed_signal("hi", 0.95)    # above threshold → sent
    _seed_signal("lo", 0.50)    # below threshold → ignored

    first = notify.notify_top_signals()
    assert first == {"candidates": 1, "sent": 1}
    assert len(captured) == 1

    # Second run (simulating the pm pass) must not re-fire the same item.
    second = notify.notify_top_signals()
    assert second == {"candidates": 0, "sent": 0}
    assert len(captured) == 1


def test_top_signals_respects_max_per_run(fresh_db, captured, monkeypatch):
    monkeypatch.setattr(notify.settings, "notify_min_score", 0.80)
    monkeypatch.setattr(notify.settings, "notify_max_per_run", 2)
    for i in range(4):
        _seed_signal(f"s{i}", 0.90)
    res = notify.notify_top_signals()
    assert res["candidates"] == 2
    assert res["sent"] == 2
