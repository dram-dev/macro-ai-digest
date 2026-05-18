"""Health checks for the digest app.

Local checks read DB + config; network probes hit each upstream API once.
Used by the `digest health` CLI command and by `scripts/smoke_test.py`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from digest import db
from digest.config import settings


class Status(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    category: str  # "local" or "network"


# ---------- Local checks ----------


def check_config() -> CheckResult:
    missing = []
    if not settings.fred_api_key:
        missing.append("FRED_API_KEY")
    if not settings.reddit_client_id:
        missing.append("REDDIT_CLIENT_ID")
    if not settings.reddit_client_secret:
        missing.append("REDDIT_CLIENT_SECRET")
    if not settings.edgar_user_agent:
        missing.append("EDGAR_USER_AGENT")
    if missing:
        return CheckResult("config", Status.FAIL, f"missing: {', '.join(missing)}", "local")
    return CheckResult("config", Status.PASS, "all required env vars set", "local")


def check_db() -> CheckResult:
    path: Path = settings.db_path
    if not path.exists():
        return CheckResult(
            "db", Status.WARN, f"not initialized at {path} — run: digest init-db", "local"
        )
    try:
        with db.get_conn() as conn:
            conn.execute("SELECT 1 FROM items LIMIT 1").fetchone()
            conn.execute("SELECT 1 FROM run_log LIMIT 1").fetchone()
        size_kb = path.stat().st_size // 1024
        return CheckResult("db", Status.PASS, f"{path} ({size_kb} KiB)", "local")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("db", Status.FAIL, f"unreadable: {type(exc).__name__}: {exc}", "local")


def check_gmail_creds() -> CheckResult:
    creds = settings.gmail_credentials_path
    token = settings.gmail_token_path
    if not creds.exists():
        return CheckResult(
            "gmail-creds", Status.FAIL, f"{creds} missing", "local"
        )
    if not token.exists():
        return CheckResult(
            "gmail-creds",
            Status.WARN,
            "creds present, token not yet — run: digest ingest gmail",
            "local",
        )
    return CheckResult("gmail-creds", Status.PASS, f"creds + token at {token.parent}", "local")


def check_last_run() -> CheckResult:
    if not settings.db_path.exists():
        return CheckResult("last-run", Status.WARN, "no DB yet", "local")
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT source, status, error FROM run_log r1 "
            "WHERE run_at = (SELECT MAX(run_at) FROM run_log r2 WHERE r2.source = r1.source)"
        ).fetchall()
    if not rows:
        return CheckResult(
            "last-run", Status.WARN, "no runs logged — run: digest ingest all", "local"
        )
    failed = [r["source"] for r in rows if r["status"] != "ok"]
    if failed:
        return CheckResult(
            "last-run", Status.FAIL, f"last run failed for: {', '.join(failed)}", "local"
        )
    return CheckResult("last-run", Status.PASS, f"{len(rows)} sources, last run ok", "local")


def check_recent_activity() -> CheckResult:
    if not settings.db_path.exists():
        return CheckResult("recent-activity", Status.WARN, "no DB yet", "local")
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM items WHERE ingested_at >= datetime('now', '-24 hours')"
        ).fetchone()
    n = row["n"] if row else 0
    if n == 0:
        return CheckResult(
            "recent-activity", Status.WARN, "no items ingested in last 24h", "local"
        )
    return CheckResult("recent-activity", Status.PASS, f"{n} items in last 24h", "local")


# ---------- Network probes ----------


def probe_fred() -> CheckResult:
    if not settings.fred_api_key:
        return CheckResult("fred", Status.FAIL, "FRED_API_KEY not set", "network")
    try:
        from fredapi import Fred

        fred = Fred(api_key=settings.fred_api_key)
        s = fred.get_series("UNRATE", observation_start="2024-01-01")
        if s is None or s.empty:
            return CheckResult("fred", Status.FAIL, "UNRATE returned empty", "network")
        return CheckResult("fred", Status.PASS, f"UNRATE latest = {s.iloc[-1]:.2f}", "network")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("fred", Status.FAIL, f"{type(exc).__name__}: {exc}", "network")


def probe_reddit() -> CheckResult:
    if not settings.reddit_client_id:
        return CheckResult("reddit", Status.FAIL, "REDDIT_CLIENT_ID not set", "network")
    try:
        import praw

        reddit = praw.Reddit(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            user_agent=settings.reddit_user_agent,
        )
        reddit.read_only = True
        post = next(reddit.subreddit("investing").top(time_filter="day", limit=1))
        return CheckResult(
            "reddit", Status.PASS, f"r/investing top = '{post.title[:50]}...'", "network"
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("reddit", Status.FAIL, f"{type(exc).__name__}: {exc}", "network")


def probe_edgar() -> CheckResult:
    if not settings.edgar_user_agent:
        return CheckResult("edgar", Status.FAIL, "EDGAR_USER_AGENT not set", "network")
    try:
        import requests

        r = requests.get(
            "https://data.sec.gov/submissions/CIK0000789019.json",  # MSFT
            headers={"User-Agent": settings.edgar_user_agent},
            timeout=20,
        )
        r.raise_for_status()
        name = r.json().get("name", "?")
        return CheckResult("edgar", Status.PASS, f"MSFT submissions API = '{name}'", "network")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("edgar", Status.FAIL, f"{type(exc).__name__}: {exc}", "network")


def probe_hn() -> CheckResult:
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
            return CheckResult("hn", Status.WARN, "no hits — possibly rate-limited", "network")
        return CheckResult(
            "hn", Status.PASS, f"Algolia = '{hits[0].get('title', '?')[:50]}...'", "network"
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult("hn", Status.FAIL, f"{type(exc).__name__}: {exc}", "network")


def probe_rss() -> list[CheckResult]:
    """One CheckResult per configured feed."""
    try:
        import feedparser
        import yaml

        root = Path(__file__).resolve().parents[2]
        feeds = yaml.safe_load((root / "config/rss_feeds.yaml").read_text())["feeds"]
    except Exception as exc:  # noqa: BLE001
        return [CheckResult("rss", Status.FAIL, f"{type(exc).__name__}: {exc}", "network")]

    results: list[CheckResult] = []
    for feed in feeds:
        name = f"rss:{feed['name'][:12]}"
        try:
            parsed = feedparser.parse(feed["url"])
            n = len(parsed.entries)
            if n > 0:
                results.append(CheckResult(name, Status.PASS, f"{n} entries", "network"))
            else:
                detail = str(parsed.bozo_exception) if parsed.bozo else "0 entries"
                results.append(CheckResult(name, Status.WARN, detail[:60], "network"))
        except Exception as exc:  # noqa: BLE001
            results.append(
                CheckResult(name, Status.FAIL, f"{type(exc).__name__}: {exc}", "network")
            )
    return results


# ---------- Orchestrator ----------


def run_all(include_network: bool = True) -> list[CheckResult]:
    """Run every check. Local first, then optional network probes."""
    results: list[CheckResult] = [
        check_config(),
        check_db(),
        check_gmail_creds(),
        check_last_run(),
        check_recent_activity(),
    ]
    if include_network:
        results.append(probe_fred())
        results.append(probe_reddit())
        results.append(probe_edgar())
        results.append(probe_hn())
        results.extend(probe_rss())
    return results
