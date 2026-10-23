"""Gmail sign-in from the phone — the link, the pasted address, and the saved token.

Offline throughout: a throwaway OAuth client in tmp_path, and Google's token
endpoint replaced by a stub that records what it was sent.
"""
from __future__ import annotations

import json
import stat
import time
from urllib.parse import parse_qs, urlparse

import pytest
from google.auth.exceptions import RefreshError
from google_auth_oauthlib.flow import Flow

from digest.ingest import gmail


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    client = tmp_path / "gmail_credentials.json"
    client.write_text(json.dumps({"installed": {
        "client_id": "test-client.apps.googleusercontent.com", "client_secret": "test-client-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"]}}))
    monkeypatch.setattr(gmail.settings, "gmail_credentials_path", client)
    monkeypatch.setattr(gmail.settings, "gmail_token_path", tmp_path / "gmail_token.json")
    return tmp_path


@pytest.fixture
def google(monkeypatch):
    """Google's token endpoint: records the exchange, answers like a Testing-mode app."""
    seen = {}

    def fetch_token(self, **kw):
        seen.update(kw, verifier=self.code_verifier)
        self.oauth2session.token = {"access_token": "test-access", "refresh_token": "test-refresh",
                                    "token_type": "Bearer", "expires_in": 3599, "expires_at": time.time() + 3599, "scope": gmail.SCOPES,
                                    "refresh_token_expires_in": 604799}
        return self.oauth2session.token
    monkeypatch.setattr(Flow, "fetch_token", fetch_token)
    return seen


def _mode(p):
    return stat.S_IMODE(p.stat().st_mode)


def _reply(url, **over):
    q = parse_qs(urlparse(url).query)
    params = {"state": q["state"][0], "code": "4/test-code", "scope": gmail.SCOPES[0], **over}
    return "http://localhost:8765/?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)


def test_the_link_asks_for_offline_access_with_pkce(secrets):
    url = gmail.begin_phone_signin()
    q = parse_qs(urlparse(url).query)
    assert q["redirect_uri"] == ["http://localhost:8765/"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"], "else Google sends no refresh token"
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"][0]
    assert q["scope"] == gmail.SCOPES
    pending = secrets / "gmail_pending.json"
    assert _mode(pending) == 0o600
    saved = json.loads(pending.read_text())
    assert saved["state"] == q["state"][0] and saved["code_verifier"]


def test_the_pasted_address_finishes_it(secrets, google):
    url = gmail.begin_phone_signin()
    verifier = json.loads((secrets / "gmail_pending.json").read_text())["code_verifier"]
    res = gmail.finish_phone_signin(_reply(url))
    assert google["code"] == "4/test-code" and google["verifier"] == verifier, "the verifier saved here"
    token = secrets / "gmail_token.json"
    assert _mode(token) == 0o600
    assert json.loads(token.read_text())["refresh_token"] == "test-refresh"
    assert not (secrets / "gmail_pending.json").exists()
    assert res == {"refresh_token": True, "ends_in_days": 7}, "a Testing app's sign-in lasts 7 days"


def test_an_older_token_file_is_made_private(secrets, google):
    token = secrets / "gmail_token.json"
    token.write_text("{}")
    token.chmod(0o644)
    gmail.finish_phone_signin(_reply(gmail.begin_phone_signin()))
    assert _mode(token) == 0o600


@pytest.mark.parametrize("over, words", [
    ({"state": "someone-elses"}, "different sign-in"),
    ({"state": None}, "different sign-in"),
    ({"code": None}, "no sign-in code"),
    ({"code": None, "error": "access_denied"}, "access_denied"),
])
def test_a_wrong_address_is_refused_and_saves_nothing(secrets, google, over, words):
    url = gmail.begin_phone_signin()
    with pytest.raises(gmail.SignInError, match=words):
        gmail.finish_phone_signin(_reply(url, **over))
    assert not google, "nothing was sent to Google"
    assert not (secrets / "gmail_token.json").exists()


def test_nothing_waiting_or_left_too_long(secrets, google, monkeypatch):
    with pytest.raises(gmail.SignInError, match="No Gmail sign-in is waiting"):
        gmail.finish_phone_signin("http://localhost:8765/?state=x&code=y")
    url = gmail.begin_phone_signin()
    later = time.time() + gmail.PENDING_TTL + 1
    monkeypatch.setattr(gmail.time, "time", lambda: later)
    with pytest.raises(gmail.SignInError, match="over an hour ago"):
        gmail.finish_phone_signin(_reply(url))
    assert not (secrets / "gmail_pending.json").exists()


def test_a_code_google_refuses_keeps_the_sign_in_open(secrets, monkeypatch):
    def refuse(self, **kw):
        raise ValueError("invalid_grant")
    monkeypatch.setattr(Flow, "fetch_token", refuse)
    url = gmail.begin_phone_signin()
    with pytest.raises(gmail.SignInError, match="didn't accept"):
        gmail.finish_phone_signin(_reply(url))
    assert (secrets / "gmail_pending.json").exists(), "the same link can be opened again"


def test_no_oauth_client_is_said(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail.settings, "gmail_credentials_path", tmp_path / "missing.json")
    with pytest.raises(gmail.SignInError, match="OAuth client"):
        gmail.begin_phone_signin()


def test_which_messages_finish_a_sign_in():
    assert gmail.looks_like_signin_reply("http://localhost:8765/?state=a&code=4/x&scope=y")
    assert gmail.looks_like_signin_reply("localhost:8765/?error=access_denied&state=a")
    assert not gmail.looks_like_signin_reply("https://www.ft.com/content/abc?code=1")
    assert not gmail.looks_like_signin_reply("http://localhost:8765/")


def test_a_dead_sign_in_says_how_to_renew(secrets, monkeypatch):
    """The daily job must never wait for a browser; its log now says what to do."""
    (secrets / "gmail_token.json").write_text(json.dumps({
        "token": "old", "refresh_token": "dead", "client_id": "c", "client_secret": "s",
        "token_uri": "https://oauth2.googleapis.com/token", "scopes": gmail.SCOPES,
        "expiry": "2026-06-17T13:02:00Z"}))

    def refused(self, request):
        raise RefreshError("invalid_grant: Bad Request")
    monkeypatch.setattr(gmail.Credentials, "refresh", refused)
    monkeypatch.setattr(gmail.InstalledAppFlow, "run_local_server",
                        lambda *a, **k: pytest.fail("the daily job opened a browser sign-in"))
    with pytest.raises(RefreshError, match="Send /gmail"):
        gmail._get_credentials()
