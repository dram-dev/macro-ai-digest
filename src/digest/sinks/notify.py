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
import logging
import sqlite3
from pathlib import Path
from urllib.parse import quote

import requests

from digest import db
from digest.config import settings

logger = logging.getLogger(__name__)

_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT = 10


class TelegramNotifier:
    """Send-only Telegram client. Disabled (no-op) unless token + chat id set."""

    def __init__(self, token: str, chat_id: str, enabled: bool) -> None:
        self.token = token
        self.chat_id = chat_id
        self.enabled = enabled and bool(token) and bool(chat_id)

    def send(self, text: str) -> bool:
        """POST one HTML message. True on success; False on no-op or any failure."""
        if not self.enabled:
            logger.debug("notify: disabled or unconfigured; skipping send")
            return False
        try:
            resp = requests.post(
                _API.format(token=self.token),
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


def _esc(s: str | None) -> str:
    """Escape the three chars Telegram HTML mode cares about (< > &)."""
    return html.escape(s or "", quote=False)


def _format_signal(row: sqlite3.Row) -> str:
    topic = _esc(row["topic"]) or "—"
    title = _esc(row["title"]) or "(no title)"
    why = _esc(row["why_it_matters"])
    score = float(row["triage_score"])
    lines = [f"⭐ <b>Top signal</b> · {topic}  (⭐ {score:.2f})", title]
    if why:
        lines.append(why[:300])
    if row["url"]:
        lines.append(f'<a href="{_esc(row["url"])}">Read source</a>')
    return "\n".join(lines)


def notify_top_signals() -> dict:
    """Push not-yet-alerted items scoring >= NOTIFY_MIN_SCORE, highest first.

    Returns {"candidates", "sent"}. Dedup is permanent per item via notify_log,
    so the am and pm runs never re-fire the same signal.
    """
    out = {"candidates": 0, "sent": 0}
    if not notifier.enabled:
        return out
    rows = db.unnotified_high_signals(settings.notify_min_score, settings.notify_max_per_run)
    out["candidates"] = len(rows)
    for row in rows:
        if notifier.send(_format_signal(row)):
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
    if not (notifier.enabled and settings.notify_brief_ping):
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
