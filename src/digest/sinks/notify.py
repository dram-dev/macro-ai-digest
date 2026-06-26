"""Telegram push notifications — terse mobile alerts for high-signal items.

A pipeline sink, mirroring the Databricks sink: a module-level singleton built
from settings that no-ops cleanly when unconfigured. Sending is one HTTPS POST
to the Telegram Bot API with parse_mode=HTML — which only needs `< > &`
escaped, far safer than MarkdownV2's dozen special chars (this repo has already
fought that class of escaping bug in its Rich markup). Nothing here raises into
the pipeline: failures are logged and swallowed so a push problem can't break a
run.

Setup: create a bot via @BotFather for TELEGRAM_BOT_TOKEN, then message it once
and read your chat id from getUpdates (or @userinfobot) for TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import html
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 10


class TelegramNotifier:
    """Telegram Bot API client. Disabled (no-op) unless token + chat id set.

    Send-only by default; `get_updates` enables the interactive ask-bot listener.
    """

    def __init__(self, token: str, chat_id: str, enabled: bool) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = enabled and bool(token) and bool(chat_id)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, text: str) -> bool:
        """POST one HTML message. True on success; False on no-op or any failure."""
        if not self.enabled:
            logger.debug("notify: disabled or unconfigured; skipping send")
            return False
        try:
            resp = requests.post(
                self._url("sendMessage"),
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.warning("notify: send failed: %s", exc)
            return False
        return True

    def send_test(self) -> bool:
        return self.send(
            "✅ <b>macro-ai-digest</b> test alert\n"
            "Telegram notifications are wired up correctly."
        )

    def send_chat_action(self, action: str = "typing") -> None:
        """Best-effort 'typing…' indicator while a reply is being prepared."""
        if not self.enabled:
            return
        try:
            requests.post(
                self._url("sendChatAction"),
                json={"chat_id": self.chat_id, "action": action},
                timeout=_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("notify: chat action failed: %s", exc)

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        """Long-poll for incoming updates. Returns [] when disabled or on error."""
        if not self.enabled:
            return []
        params: dict = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        try:
            resp = requests.get(
                self._url("getUpdates"), params=params, timeout=timeout + 10
            )
            resp.raise_for_status()
            return resp.json().get("result", [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("notify: get_updates failed: %s", exc)
            return []


def _esc(s: str | None) -> str:
    """Escape the three chars Telegram HTML mode cares about (< > &)."""
    return html.escape(s or "", quote=False)


def _row_get(row: sqlite3.Row, key: str, default=None):
    """Column access that tolerates both sqlite3.Row and plain dicts (tests)."""
    try:
        val = row[key]
    except (KeyError, IndexError):
        return default
    return default if val is None else val


_SENTIMENT_EMOJI = {"bullish": "🟢", "bearish": "🔴", "neutral": "⚪"}

# Pretty display names for sources without a per-feed label in metadata.
_SOURCE_NAMES = {
    "hn": "Hacker News", "edgar": "SEC EDGAR", "fred": "FRED", "reddit": "Reddit",
    "arxiv": "arXiv", "huggingface": "Hugging Face", "cboe": "CBOE", "cftc": "CFTC",
    "yahoo": "Yahoo Finance", "insider": "Insider Tx", "ftd": "FTD",
    "calendar": "Calendar", "gmail": "Economist", "clipped": "Clipped",
    "substack": "Substack", "rss": "RSS",
}


def _source_name(source: str | None, metadata_json: str | None) -> str:
    """The publication label (RSS/Substack feed name) or a pretty source name."""
    if metadata_json:
        try:
            feed = json.loads(metadata_json).get("feed")
        except (ValueError, TypeError):
            feed = None
        if feed:
            return str(feed)
    return _SOURCE_NAMES.get(source or "", (source or "").title() or "—")


def _fmt_date(val) -> str | None:
    """Date portion of a stored published_at (YYYY-MM-DD), or None."""
    if not val:
        return None
    s = str(val)
    return s[:10] if len(s) >= 10 else s


def _format_signal(row: sqlite3.Row, storyline: str | None = None) -> str:
    topic = _esc(row["topic"]) or "—"
    title = _esc(row["title"]) or "(no title)"
    why = _esc(_row_get(row, "why_it_matters"))
    score = float(row["triage_score"])
    # Header: score keeps its star; no redundant leading star.
    lines = [f"<b>Top signal</b> · {topic}  (⭐ {score:.2f})", title]
    if why:
        lines.append(why[:300])

    # Meta line: source · date · sentiment · storyline
    meta = [_esc(_source_name(_row_get(row, "source"), _row_get(row, "metadata_json")))]
    pub = _fmt_date(_row_get(row, "published_at"))
    if pub:
        meta.append(_esc(pub))
    label = _row_get(row, "sentiment_label")
    if label:
        meta.append(f"{_SENTIMENT_EMOJI.get(label, '')} {_esc(label)}".strip())
    if storyline:
        meta.append(f"📖 {_esc(storyline)}")
    lines.append(f"<i>{' · '.join(meta)}</i>")

    if _row_get(row, "url"):
        lines.append(f'<a href="{_esc(row["url"])}">Read source</a>')
    return "\n".join(lines)


def _pushing_allowed(now: datetime | None = None) -> bool:
    """True when the local hour is inside the allowed (non-quiet) window.

    Allowed when end <= hour < start (e.g. 8 <= h < 22). A suppressed run isn't
    a loss: the next run inside the window re-picks the items (within lookback).
    """
    h = (now or datetime.now()).hour
    start = settings.notify_quiet_start_hour
    end = settings.notify_quiet_end_hour
    if end <= start:
        return end <= h < start
    return h >= end or h < start  # config with a window that wraps midnight


def notify_top_signals() -> dict:
    """Push net-new items scoring >= NOTIFY_MIN_SCORE, highest first.

    Returns {"candidates", "sent"}. Dedup is permanent per item via notify_log,
    a recency window keeps it to genuine net-new signals, and quiet hours
    suppress the whole step (sending + recording) outside the allowed window.
    """
    out = {"candidates": 0, "sent": 0}
    if not notifier.enabled or not _pushing_allowed():
        return out
    rows = db.unnotified_high_signals(
        settings.notify_min_score,
        settings.notify_max_per_run,
        lookback_hours=settings.notify_lookback_hours,
    )
    out["candidates"] = len(rows)
    stories = db.storyline_names_for_items([r["id"] for r in rows]) if rows else {}
    for row in rows:
        if notifier.send(_format_signal(row, storyline=stories.get(row["id"]))):
            db.record_notification(f"signal:{row['id']}", "signal", row["id"])
            out["sent"] += 1
    return out


def _brief_link(date_iso: str) -> str | None:
    """obsidian:// deep link to the day's Brief note, if a vault is configured."""
    if not settings.obsidian_vault_path:
        return None
    vault_name = Path(settings.obsidian_vault_path).name
    file_path = f"{settings.obsidian_digest_dir}/Brief/{date_iso} Brief"
    return f"obsidian://open?vault={quote(vault_name)}&file={quote(file_path)}"


def notify_brief_ready(date_iso: str) -> bool:
    """Optional once-per-run 'Brief ready' ping (off unless NOTIFY_BRIEF_PING)."""
    if not (notifier.enabled and settings.notify_brief_ping and _pushing_allowed()):
        return False
    top = db.items_for_signals()[:5]
    lines = [f"📰 <b>Digest Brief ready</b> · {_esc(date_iso)}"]
    lines += [f"• {_esc(r['title'])}" for r in top]
    link = _brief_link(date_iso)
    if link:
        lines.append(f'<a href="{_esc(link)}">Open Brief</a>')
    return notifier.send("\n".join(lines))


notifier = TelegramNotifier(
    token=settings.telegram_bot_token,
    chat_id=settings.telegram_chat_id,
    enabled=settings.notify_enabled,
)
