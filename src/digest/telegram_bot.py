"""Interactive Telegram ask-bot — query the archive from your phone.

A long-polling listener (no public endpoint needed): it reads incoming messages
via getUpdates, runs each through the RAG `answer_question`, and replies in the
same chat. Built to run as a launchd daemon alongside the MLX server.

Security: it answers ONLY the configured TELEGRAM_CHAT_ID. Messages from any
other chat (someone who stumbled onto the bot) are logged and ignored.
"""
from __future__ import annotations

import html
import logging
import time

from digest import ask
from digest.config import settings
from digest.sinks.notify import notifier

logger = logging.getLogger(__name__)

_MAX_MSG = 4000  # Telegram hard limit is 4096; leave headroom
_HELP = (
    "🔎 <b>Ask the digest archive</b>\n"
    "Send any question and I'll answer from the kept items, with sources.\n"
    "Examples:\n"
    "• <i>What's the latest on hyperscaler capex?</i>\n"
    "• <i>Any signals on the 2s10s spread?</i>"
)


def _esc(s: str | None) -> str:
    return html.escape(s or "", quote=False)


def _format_reply(result: dict) -> str:
    """Render an answer + numbered sources as one HTML message (length-capped)."""
    answer = result.get("answer")
    sources = result.get("sources") or []
    if not sources:
        return "No matching items in the archive for that one."

    parts = [_esc(answer) if answer else "<i>(synthesis unavailable — top matches below)</i>"]
    parts.append("\n<b>Sources</b>")
    for n, s in enumerate(sources, 1):
        date = (s.get("published_at") or "")[:10]
        title = _esc(s.get("title"))
        url = s.get("url")
        line = f"[{n}] {title} <i>({_esc(s.get('source'))} · {date})</i>"
        if url:
            line += f' — <a href="{_esc(url)}">link</a>'
        parts.append(line)
    msg = "\n".join(parts)
    return msg[:_MAX_MSG]


def _handle_message(text: str) -> bool:
    """Process one inbound question and reply. Returns True if a reply was sent."""
    text = (text or "").strip()
    if not text:
        return False
    if text.startswith("/"):  # /start, /help, etc.
        notifier.send(_HELP)
        return True
    notifier.send_chat_action("typing")
    try:
        result = ask.answer_question(text)
        notifier.send(_format_reply(result))
    except ask.AskError as exc:
        notifier.send(f"⚠️ {_esc(str(exc))}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("ask-bot: failed to answer")
        notifier.send(f"⚠️ Something went wrong answering that: {_esc(str(exc))}")
    return True


def _is_authorized(update: dict) -> bool:
    """Only the configured chat id may talk to the bot."""
    chat_id = (update.get("message") or {}).get("chat", {}).get("id")
    return chat_id is not None and str(chat_id) == str(settings.telegram_chat_id)


def _drain_backlog() -> int | None:
    """Skip any messages queued before startup; return the next offset to poll."""
    updates = notifier.get_updates(timeout=0)
    if not updates:
        return None
    last = updates[-1]["update_id"]
    logger.info("ask-bot: skipped %d backlog update(s)", len(updates))
    return last + 1


def run_listener(poll_timeout: int = 30) -> None:
    """Block forever, answering authorized questions. Intended for a daemon."""
    if not notifier.enabled:
        raise RuntimeError(
            "Telegram not configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID."
        )
    logger.info("ask-bot: listening (chat_id=%s)", settings.telegram_chat_id)
    offset = _drain_backlog()
    while True:
        updates = notifier.get_updates(offset=offset, timeout=poll_timeout)
        for u in updates:
            offset = u["update_id"] + 1
            if not _is_authorized(u):
                logger.warning("ask-bot: ignoring update from unauthorized chat")
                continue
            text = (u.get("message") or {}).get("text")
            _handle_message(text)
        if not updates:
            time.sleep(1)  # gentle floor when long-poll returns empty/errs
