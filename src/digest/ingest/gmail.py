"""Gmail ingestor — fetches messages under the Economist digest label.

First run opens a browser for OAuth consent. Subsequent runs use the cached token.
From a phone instead, the ask-bot's /gmail runs the sign-in (begin_phone_signin).
Scope is gmail.readonly — we never modify or delete mail.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MAX_MESSAGES_PER_RUN = 300


def _get_credentials() -> Credentials:
    """Load cached creds or run interactive OAuth flow."""
    token_path = settings.gmail_token_path
    creds_path = settings.gmail_credentials_path

    creds: Credentials | None = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as exc:
            # Never fall back to the browser here: the daily job has no one at the Mac.
            raise RefreshError(f"Gmail sign-in no longer works ({exc}). Send /gmail to the "
                               "digest bot to sign in again from your phone.") from exc
    else:
        if not creds_path.exists():
            raise FileNotFoundError(
                f"Gmail OAuth credentials not found at {creds_path}. "
                "Download from Google Cloud Console (Desktop app OAuth client) and save there."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        creds = flow.run_local_server(port=0)

    _write_private(token_path, creds.to_json())
    return creds


def _write_private(path: Path, text: str) -> None:
    """Write a file only this user can read, from the first byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)                    # an older file keeps its mode through O_CREAT
    with os.fdopen(fd, "w") as f:
        f.write(text)


# ── sign-in from your phone ─────────────────────────────────────────────────
# The saved sign-in died in June 2026 and nothing noticed: the OAuth client's
# consent screen is in Testing (secrets/README.md), where Google ends a sign-in
# after 7 days, and run_local_server needs a browser on this Mac. The ask-bot's
# /gmail sends Google's link instead. After you approve on the phone, Google
# redirects to a localhost address that won't load there; its URL carries a
# single-use code, and pasting it back to the bot exchanges it here. The PKCE
# verifier never leaves this Mac, so the code is useless to anyone else.
PHONE_REDIRECT = "http://localhost:8765/"
PENDING_NAME = "gmail_pending.json"
PENDING_TTL = 3600                          # a sign-in left this long is started afresh


class SignInError(RuntimeError):
    """A phone sign-in that can't go on. The message is written for the person signing in."""


def _pending_path() -> Path:
    return settings.gmail_token_path.with_name(PENDING_NAME)


def looks_like_signin_reply(text: str) -> bool:
    """The address Google sent the phone to, pasted back into the chat."""
    return "localhost:8765" in text and ("code=" in text or "error=" in text)


def begin_phone_signin() -> str:
    """Start a sign-in to finish on the phone. Returns Google's link to open there."""
    creds_path = settings.gmail_credentials_path
    if not creds_path.exists():
        raise SignInError(f"The Google OAuth client isn't at {creds_path}. See secrets/README.md.")
    flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES, redirect_uri=PHONE_REDIRECT,
                                                     autogenerate_code_verifier=True)
    # prompt=consent: Google issues a refresh token only on a consent screen.
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    _write_private(_pending_path(), json.dumps({"state": state, "code_verifier": flow.code_verifier,
                                                "redirect_uri": PHONE_REDIRECT, "t": time.time()}))
    return url


def finish_phone_signin(response: str) -> dict:
    """Exchange the pasted address for a sign-in and save it. Returns what to tell the
    person: whether a refresh token came back, and in how many days Google will end
    the sign-in when it says so (Testing apps: 7)."""
    pending_path = _pending_path()
    if not pending_path.exists():
        raise SignInError("No Gmail sign-in is waiting. Send /gmail to start one.")
    pending = json.loads(pending_path.read_text())
    if time.time() - float(pending.get("t") or 0) > PENDING_TTL:
        pending_path.unlink(missing_ok=True)
        raise SignInError("That sign-in was started over an hour ago. Send /gmail to start again.")
    text = response.strip().strip("'\"<>")
    query = parse_qs(urlparse(text if "://" in text else "http://x/?" + text.split("?", 1)[-1]).query)
    if query.get("error"):
        raise SignInError(f"Google didn't sign you in ({query['error'][0]}). Send /gmail to try again.")
    if (query.get("state") or [""])[0] != pending["state"]:
        raise SignInError("That address belongs to a different sign-in. Send /gmail to start again.")
    code = (query.get("code") or [""])[0]
    if not code:
        raise SignInError("There's no sign-in code in that address. Copy the whole address from the browser.")
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(settings.gmail_credentials_path), SCOPES, redirect_uri=pending["redirect_uri"],
        code_verifier=pending["code_verifier"], autogenerate_code_verifier=False)
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # noqa: BLE001 — the pending stays, so the same link can be opened again
        raise SignInError(f"Google didn't accept that code ({type(exc).__name__}). Open the link again, "
                          "or send /gmail for a new one.") from exc
    creds = flow.credentials
    _write_private(settings.gmail_token_path, creds.to_json())
    pending_path.unlink(missing_ok=True)
    lasts = (flow.oauth2session.token or {}).get("refresh_token_expires_in")
    return {"refresh_token": bool(creds.refresh_token),
            "ends_in_days": round(float(lasts) / 86400) if lasts else None}


def _extract_plain_text(payload: dict[str, Any]) -> str:
    """Recursively dig out text/plain from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data")
    if mime == "text/plain" and data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        text = _extract_plain_text(part)
        if text:
            return text
    # Fallback: extract and strip from HTML
    if mime == "text/html" and data:
        html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", " ", html)
    return ""


def _header(headers: list[dict[str, str]], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _parse_date(date_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        return parsedate_to_datetime(date_str)
    except (TypeError, ValueError):
        return None


class GmailIngestor(IngestorBase):
    name = "gmail"

    def fetch(self) -> list[IngestedItem]:
        creds = _get_credentials()
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        query = f'label:"{settings.gmail_label}" newer_than:{settings.gmail_lookback_days}d'
        msg_refs: list[dict] = []
        page_token: str | None = None
        while len(msg_refs) < MAX_MESSAGES_PER_RUN:
            resp = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    q=query,
                    maxResults=min(500, MAX_MESSAGES_PER_RUN - len(msg_refs)),
                    pageToken=page_token,
                )
                .execute()
            )
            msg_refs.extend(resp.get("messages", []) or [])
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        logger.info("gmail: found %d messages matching query", len(msg_refs))

        items: list[IngestedItem] = []
        for ref in msg_refs:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=ref["id"], format="full")
                .execute()
            )
            headers = msg.get("payload", {}).get("headers", [])
            subject = _header(headers, "Subject") or "(no subject)"
            sender = _header(headers, "From")
            date_hdr = _header(headers, "Date")

            text = _extract_plain_text(msg.get("payload", {})).strip()
            items.append(
                IngestedItem(
                    source=self.name,
                    source_id=msg["id"],
                    title=subject,
                    author=sender,
                    content=text,
                    published_at=_parse_date(date_hdr),
                    metadata={
                        "thread_id": msg.get("threadId"),
                        "snippet": msg.get("snippet"),
                        "label_ids": msg.get("labelIds", []),
                    },
                )
            )
        return items
