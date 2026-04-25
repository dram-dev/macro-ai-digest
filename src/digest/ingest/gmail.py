"""Gmail ingestor — fetches messages under the Economist digest label.

First run opens a browser for OAuth consent. Subsequent runs use the cached token.
Scope is gmail.readonly — we never modify or delete mail.
"""
from __future__ import annotations

import base64
import logging
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from digest.config import settings
from digest.ingest.base import IngestedItem, IngestorBase

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
MAX_MESSAGES_PER_RUN = 50


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
        creds.refresh(Request())
    else:
        if not creds_path.exists():
            raise FileNotFoundError(
                f"Gmail OAuth credentials not found at {creds_path}. "
                "Download from Google Cloud Console (Desktop app OAuth client) and save there."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        creds = flow.run_local_server(port=0)

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    return creds


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

        query = f'label:"{settings.gmail_label}" newer_than:14d'
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=MAX_MESSAGES_PER_RUN)
            .execute()
        )
        msg_refs = resp.get("messages", []) or []
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
