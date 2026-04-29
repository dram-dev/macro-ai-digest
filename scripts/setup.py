"""Interactive credential setup wizard.

Walks you through each credential, validates format, writes to .env.
Does NOT test live API calls — that's smoke_test.py's job.

Usage:
    uv run python scripts/setup.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
SECRETS_DIR = ROOT / "secrets"

BANNER = """
════════════════════════════════════════════════════════════════════
  macro-ai-digest — credential setup wizard
════════════════════════════════════════════════════════════════════

This will walk you through 3 required + 1 optional credential:
  1. FRED API key (free, required)
  2. SEC EDGAR user-agent (no signup, just format, required)
  3. Gmail OAuth (Google Cloud Console; opens browser on first real run)
  4. Reddit API (OPTIONAL — default ingestor uses public JSON, no auth)

Your answers are written to .env. Ctrl-C to abort at any point.
"""


def prompt(label: str, default: str = "", secret: bool = False, validator=None) -> str:
    """Prompt until a valid value is given."""
    while True:
        suffix = f" [{default}]" if default else ""
        raw = input(f"  {label}{suffix}: ").strip()
        if not raw and default:
            raw = default
        if not raw:
            print("    ↳ required, please enter a value")
            continue
        if validator:
            ok, msg = validator(raw)
            if not ok:
                print(f"    ↳ {msg}")
                continue
        return raw


def _v_reddit_id(s: str):
    if re.fullmatch(r"[A-Za-z0-9_-]{14,30}", s):
        return True, ""
    return False, "expected ~14–22 char alphanumeric string from reddit.com/prefs/apps"


def _v_reddit_secret(s: str):
    if re.fullmatch(r"[A-Za-z0-9_-]{20,40}", s):
        return True, ""
    return False, "expected a 20–30 char secret string"


def _v_fred_key(s: str):
    if re.fullmatch(r"[a-f0-9]{32}", s):
        return True, ""
    return False, "FRED keys are 32 hex characters"


def _v_edgar_ua(s: str):
    # Need a name and an email address in there
    if "@" in s and len(s.split()) >= 2:
        return True, ""
    return False, "SEC wants 'Your Name your.email@example.com'"


def _v_redditor(s: str):
    if re.fullmatch(r"[A-Za-z0-9_-]{3,20}", s):
        return True, ""
    return False, "reddit usernames are 3–20 chars, alphanumeric/underscore/hyphen"


def load_existing_env() -> dict[str, str]:
    """Merge .env (if exists) over .env.example for defaults."""
    env: dict[str, str] = {}
    for source in (ENV_EXAMPLE, ENV_PATH):
        if not source.exists():
            continue
        for line in source.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env


def write_env(env: dict[str, str]) -> None:
    """Write .env preserving comments from .env.example where possible."""
    lines: list[str] = []
    written: set[str] = set()

    if ENV_EXAMPLE.exists():
        for line in ENV_EXAMPLE.read_text().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                lines.append(line)
                continue
            if "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                val = env.get(key, "")
                lines.append(f"{key}={val}")
                written.add(key)
            else:
                lines.append(line)

    # Pick up any keys not in the template
    for k, v in env.items():
        if k not in written:
            lines.append(f"{k}={v}")

    ENV_PATH.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_PATH, 0o600)


def main() -> int:
    print(BANNER)
    env = load_existing_env()

    # ── FRED ─────────────────────────────────────────────────────
    print("\n── [1/4] FRED API key (required) ───────────────────────────────")
    print("  1. Open: https://fred.stlouisfed.org/docs/api/api_key.html")
    print("  2. Sign in / create account")
    print("  3. Click 'Request API Key'   (it issues a 32-char hex string)\n")

    env["FRED_API_KEY"] = prompt(
        "FRED API key", env.get("FRED_API_KEY", ""), validator=_v_fred_key
    )

    # ── EDGAR user-agent ─────────────────────────────────────────
    print("\n── [2/4] SEC EDGAR user-agent (required) ──────────────────────")
    print("  SEC requires a real name + email in the user-agent for API access.")
    print("  No signup. Just format: 'Your Name your.email@example.com'\n")

    env["EDGAR_USER_AGENT"] = prompt(
        "EDGAR user-agent", env.get("EDGAR_USER_AGENT", ""), validator=_v_edgar_ua
    )

    # ── Gmail OAuth ──────────────────────────────────────────────
    print("\n── [3/4] Gmail OAuth (required) ────────────────────────────────")
    print("  This one needs a JSON file, not a pasted credential.")
    print("  1. Open: https://console.cloud.google.com/apis/credentials")
    print("  2. Create project 'macro-ai-digest' if you don't have one")
    print("  3. Enable Gmail API for the project")
    print("  4. OAuth consent screen: 'External', Test user = your email")
    print("  5. Credentials → 'Create Credentials' → OAuth client ID → 'Desktop app'")
    print("  6. Download the JSON. Rename to 'gmail_credentials.json'.")
    print(f"  7. Save it to: {SECRETS_DIR}/gmail_credentials.json\n")

    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    creds_file = SECRETS_DIR / "gmail_credentials.json"
    if creds_file.exists():
        print(f"  ✓ Found {creds_file.name} already in secrets/")
    else:
        print(f"  ⚠ {creds_file.name} not yet in secrets/. Drop it there later;")
        print("    `digest ingest gmail` will prompt the OAuth flow on first run.")

    # ── Reddit (optional) ────────────────────────────────────────
    print("\n── [4/4] Reddit (OPTIONAL) ─────────────────────────────────────")
    print("  Reddit now requires pre-approval for personal-use API scripts.")
    print("  The default ingestor uses the public JSON endpoint and needs no")
    print("  credentials — only fill these in if your Reddit API request was")
    print("  approved. You can re-run this wizard later to add them.\n")

    skip_reddit = input("  Skip Reddit credentials? [Y/n]: ").strip().lower()
    if skip_reddit in ("", "y", "yes"):
        # Set a default User-Agent so the JSON path identifies cleanly
        if not env.get("REDDIT_USER_AGENT") or "your_username" in env.get(
            "REDDIT_USER_AGENT", ""
        ):
            handle = input(
                "  Reddit username (for User-Agent header, optional, just hit enter to skip): "
            ).strip()
            if handle:
                env["REDDIT_USER_AGENT"] = f"macro-ai-digest/0.1 by u/{handle}"
            else:
                env["REDDIT_USER_AGENT"] = "macro-ai-digest/0.1 (JSON mode)"
        env["REDDIT_USE_PRAW"] = "false"
        # Leave any prior CLIENT_ID/SECRET in place; they're harmless in JSON mode
        print("  ✓ Using public JSON mode (no auth)")
    else:
        env["REDDIT_CLIENT_ID"] = prompt(
            "Reddit client ID", env.get("REDDIT_CLIENT_ID", ""), validator=_v_reddit_id
        )
        env["REDDIT_CLIENT_SECRET"] = prompt(
            "Reddit client secret",
            env.get("REDDIT_CLIENT_SECRET", ""),
            validator=_v_reddit_secret,
        )
        username = prompt("Reddit username (for user-agent)", validator=_v_redditor)
        env["REDDIT_USER_AGENT"] = f"macro-ai-digest/0.1 by u/{username}"
        env["REDDIT_USE_PRAW"] = "true"
        print("  ✓ PRAW mode enabled")

    # ── Write ────────────────────────────────────────────────────
    write_env(env)
    print(f"\n✓ Wrote {ENV_PATH} (mode 0600)")

    # ── Gmail filter reminder ────────────────────────────────────
    print("\n── One more manual step: Gmail filter ──────────────────────────")
    print("  In Gmail UI (gmail.com), create a filter:")
    print("    Matches: from:(newsletters@economist.com OR noreply@economist.com")
    print("             OR newsletter@e.economist.com OR news@e.economist.com)")
    print("    Action:  Apply label 'Digest/Economist'")
    print("    Also:    check 'Apply filter to matching conversations'\n")

    print("Next: uv run python scripts/smoke_test.py\n")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n(aborted — no changes written on this run)")
        sys.exit(130)
