"""Smoke test — one minimal call per source to confirm credentials work.

Does NOT write to the DB. Reports a pass/fail line per source.
Thin wrapper around `digest.health` network probes for backwards compatibility.

Usage:
    uv run python scripts/smoke_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from digest import health  # noqa: E402

ICONS = {
    health.Status.PASS: "\033[32m✓\033[0m",
    health.Status.WARN: "\033[33m⚠\033[0m",
    health.Status.FAIL: "\033[31m✗\033[0m",
}


def line(result: health.CheckResult) -> None:
    print(f"  {ICONS[result.status]} {result.name:14} {result.detail}")


def main() -> int:
    print("\n  Running smoke tests...\n")
    results = [
        health.probe_fred(),
        health.probe_reddit(),
        health.probe_edgar(),
        health.probe_hn(),
        *health.probe_rss(),
        health.check_gmail_creds(),
    ]
    for r in results:
        line(r)
    passed = sum(1 for r in results if r.status == health.Status.PASS)
    total = len(results)
    print(f"\n  {passed}/{total} passed\n")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
