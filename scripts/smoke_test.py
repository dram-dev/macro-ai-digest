"""Smoke test — one minimal call per source to confirm credentials work.

Does NOT write to the DB. Reports a pass/fail line per source.

Usage:
    uv run python scripts/smoke_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from digest.config import settings  # noqa: E402

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m⚠\033[0m"


def line(icon: str, source: str, detail: str = "") -> None:
    print(f"  {icon} {source:12} {detail}")


def test_fred() -> bool:
    try:
        if not settings.fred_api_key:
            line(FAIL, "fred", "FRED_API_KEY not set in .env")
            return False
        from fredapi import Fred

        fred = Fred(api_key=settings.fred_api_key)
        s = fred.get_series("UNRATE", observation_start="2024-01-01")
        if s is None or s.empty:
            line(FAIL, "fred", "UNRATE returned empty")
            return False
        line(PASS, "fred", f"UNRATE latest = {s.iloc[-1]:.2f}")
        return True
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "fred", f"{type(exc).__name__}: {exc}")
        return False


def test_reddit() -> bool:
    try:
        if not settings.reddit_client_id:
            line(FAIL, "reddit", "REDDIT_CLIENT_ID not set in .env")
            return False
        import praw

        reddit = praw.Reddit(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            user_agent=settings.reddit_user_agent,
        )
        reddit.read_only = True
        post = next(reddit.subreddit("investing").top(time_filter="day", limit=1))
        line(PASS, "reddit", f"r/investing top = '{post.title[:50]}...'")
        return True
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "reddit", f"{type(exc).__name__}: {exc}")
        return False


def test_edgar() -> bool:
    try:
        if not settings.edgar_user_agent:
            line(FAIL, "edgar", "EDGAR_USER_AGENT not set in .env")
            return False
        import requests

        r = requests.get(
            "https://data.sec.gov/submissions/CIK0000789019.json",  # MSFT
            headers={"User-Agent": settings.edgar_user_agent},
            timeout=20,
        )
        r.raise_for_status()
        name = r.json().get("name", "?")
        line(PASS, "edgar", f"MSFT submissions API = '{name}'")
        return True
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "edgar", f"{type(exc).__name__}: {exc}")
        return False


def test_hn() -> bool:
    try:
        import requests

        r = requests.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={"query": "LLM", "tags": "story", "hitsPerPage": 1},
            timeout=15,
        )
        r.raise_for_status()
        hits = r.json().get("hits", [])
        if not hits:
            line(WARN, "hn", "no hits — possibly rate-limited, try again")
            return False
        line(PASS, "hn", f"Algolia = '{hits[0].get('title', '?')[:50]}...'")
        return True
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "hn", f"{type(exc).__name__}: {exc}")
        return False


def test_rss() -> bool:
    """Validate each configured feed parses and returns entries."""
    try:
        import feedparser
        import yaml

        feeds = yaml.safe_load((ROOT / "config/rss_feeds.yaml").read_text())["feeds"]
        any_ok = False
        for feed in feeds:
            name = feed["name"]
            url = feed["url"]
            parsed = feedparser.parse(url)
            n = len(parsed.entries)
            if n > 0:
                line(PASS, f"rss:{name[:8]}", f"{n} entries")
                any_ok = True
            else:
                detail = str(parsed.bozo_exception) if parsed.bozo else "0 entries"
                line(WARN, f"rss:{name[:8]}", detail[:60])
        return any_ok
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "rss", f"{type(exc).__name__}: {exc}")
        return False


def test_gmail() -> bool:
    """Check creds file exists. Does not trigger OAuth (needs browser)."""
    try:
        creds = settings.gmail_credentials_path
        token = settings.gmail_token_path
        if not creds.exists():
            line(FAIL, "gmail", f"{creds} missing — download from Google Cloud Console")
            return False
        if not token.exists():
            line(WARN, "gmail", "creds present, token not yet. Run: digest ingest gmail")
            return True  # not a failure — OAuth is a manual step
        line(PASS, "gmail", f"creds + token both present at {token.parent}")
        return True
    except Exception as exc:  # noqa: BLE001
        line(FAIL, "gmail", f"{type(exc).__name__}: {exc}")
        return False


def main() -> int:
    print("\n  Running smoke tests...\n")
    results = {
        "fred":   test_fred(),
        "reddit": test_reddit(),
        "edgar":  test_edgar(),
        "hn":     test_hn(),
        "rss":    test_rss(),
        "gmail":  test_gmail(),
    }
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n  {passed}/{total} passed\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
