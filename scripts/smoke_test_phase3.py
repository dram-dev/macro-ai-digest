"""Phase 3 smoke test — Obsidian writer exercises a temp vault, then validates output.

Builds a temporary vault, writes synthetic items into a temp DB, runs the
writer, and asserts file contents and idempotency. Does NOT touch the real
Obsidian vault.

Usage:
    uv run python scripts/smoke_test_phase3.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def line(icon: str, label: str, detail: str = "") -> None:
    print(f"  {icon} {label:36} {detail}")


def seed_db(db_path: Path) -> None:
    """Populate a fresh Phase-2-shaped DB with synthetic test items."""
    from digest import db as dbmod

    os.environ["DB_PATH"] = str(db_path)
    # Force settings reload so DB_PATH takes effect
    from digest import config as cfgmod
    cfgmod.settings = cfgmod.Settings()
    dbmod.settings = cfgmod.settings

    dbmod.init_db(db_path)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = [
        # Summarized — fed_markets, high score
        (
            "rss", "test_001", "https://example.com/fed",
            "Fed signals patience on rate cuts",
            "Reuters", "Body content...",
            f"{today}T08:00:00+00:00",
            f"{today} 08:30:00",
            "{}", "fed_markets",
            "Fed officials emphasized data dependence at Jackson Hole, with several "
            "members pushing back on market expectations of imminent cuts.",
            "Suggests near-term rate paths priced into Fed funds futures may be "
            "too aggressive; watch 2s10s for confirmation.",
            "high", '["2s10s spread inversion", "Powell Jackson Hole 2026"]',
            0.92, "keep",
            f"{today} 08:31:00", f"{today} 08:32:00",
        ),
        # Summarized — ai_capex, medium score
        (
            "edgar", "MSFT:test_002", "https://sec.gov/test",
            "MSFT 8-K capex guidance update",
            "Microsoft Corporation", "Filing body...",
            f"{today}T09:00:00+00:00",
            f"{today} 09:15:00",
            '{"ticker":"MSFT","form":"8-K"}', "ai_capex",
            "Microsoft pre-announced FY26 capex guide of $X, ahead of consensus.",
            "Confirms hyperscaler buildout pace remains strong; bullish for NVDA H1 "
            "expectations and supports the AI infrastructure thesis.",
            "high", '["NVIDIA Q3 expectations", "hyperscaler 2026 capex"]',
            0.85, "keep",
            f"{today} 09:16:00", f"{today} 09:17:00",
        ),
        # Summarized — ai_thinkers
        (
            "rss", "test_003", "https://karpathy.bearblog.dev/post",
            "Verifiability is the new bottleneck",
            "Andrej Karpathy", "Essay body...",
            f"{today}T10:00:00+00:00",
            f"{today} 10:30:00",
            "{}", "ai_thinkers",
            "Karpathy argues that as model capability scales, the hard constraint "
            "shifts from training to verification of outputs at runtime.",
            "Implies tooling and eval infrastructure are the next investment frontier; "
            "matters for vendor selection in your AI strategy.",
            "high", "[]",
            0.88, "keep",
            f"{today} 10:31:00", f"{today} 10:32:00",
        ),
        # Kept but not summarized (cap overflow)
        (
            "reddit", "test_004", "https://reddit.com/r/test",
            "China PMI surprises to the downside",
            "user_x", None,
            f"{today}T11:00:00+00:00",
            f"{today} 11:00:00",
            "{}", "china",
            None, None, None, None,
            0.71, "keep",
            f"{today} 11:01:00", None,
        ),
        # Dropped item — should NOT appear in daily note
        (
            "hn", "test_005", "https://news.ycombinator.com/item",
            "Show HN: my new todo app",
            "anon", None,
            f"{today}T12:00:00+00:00",
            f"{today} 12:00:00",
            "{}", "other",
            None, None, None, None,
            0.10, "drop",
            f"{today} 12:01:00", None,
        ),
    ]
    sql = """
        INSERT INTO items
            (source, source_id, url, title, author, content,
             published_at, ingested_at, metadata_json,
             topic, summary, why_it_matters, confidence, see_also,
             triage_score, triage_decision, triaged_at, summarized_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    with sqlite3.connect(db_path) as conn:
        conn.executemany(sql, rows)
        conn.commit()


def main() -> int:
    print("\n  Phase 3 smoke tests — Obsidian writer\n")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        vault = tmp_path / "TestVault"
        vault.mkdir()
        db_path = tmp_path / "state.db"

        # Point env at temp vault and DB
        os.environ["OBSIDIAN_VAULT_PATH"] = str(vault)
        os.environ["OBSIDIAN_DIGEST_DIR"] = "80 Digest"
        os.environ["DB_PATH"] = str(db_path)

        # Seed DB
        try:
            seed_db(db_path)
            line(PASS, "seeded test DB", str(db_path))
        except Exception as exc:  # noqa: BLE001
            line(FAIL, "seeded test DB", f"{type(exc).__name__}: {exc}")
            return 1

        # Run publish
        try:
            from digest.obsidian import publish

            result = publish()
            line(PASS, "publish ran", f"daily_items={result['daily_items']}")
        except Exception as exc:  # noqa: BLE001
            line(FAIL, "publish ran", f"{type(exc).__name__}: {exc}")
            return 1

        digest_root = vault / "80 Digest"

        # Daily note
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily = digest_root / "Daily" / f"{today}.md"
        if daily.exists():
            text = daily.read_text(encoding="utf-8")
            line(PASS, "daily note written", f"{len(text)} bytes")
        else:
            line(FAIL, "daily note written", "missing")
            return 1

        # Content checks
        checks = [
            ("Fed signals patience" in text, "fed_markets summary present"),
            ("MSFT 8-K" in text, "ai_capex summary present"),
            ("Karpathy" in text, "ai_thinkers summary present"),
            ("Why it matters:" in text, "why-it-matters present"),
            ("🟢 high" in text, "confidence badge rendered"),
            ("Kept — not summarized" in text, "kept-unsummarized section present"),
            ("China PMI" in text, "kept-unsummarized item present"),
            ("Show HN: my new todo app" not in text, "dropped item absent"),
            ("[[Fed & Markets]]" in text, "wikilink to topic archive"),
            ("---\ndate:" in text, "YAML frontmatter present"),
        ]
        all_passed = True
        for passed, label_str in checks:
            if passed:
                line(PASS, label_str)
            else:
                line(FAIL, label_str)
                all_passed = False

        # Topic archives
        topics_dir = digest_root / "Topics"
        expected = ["Fed & Markets.md", "AI Capex.md", "AI Thinkers.md"]
        for fname in expected:
            tpath = topics_dir / fname
            if tpath.exists():
                t_text = tpath.read_text(encoding="utf-8")
                has_index = "digest:index:begin" in t_text
                has_marker = "digest:item:" in t_text
                if has_index and has_marker:
                    line(PASS, f"topic archive: {fname}")
                else:
                    line(FAIL, f"topic archive: {fname}", "missing markers")
                    all_passed = False
            else:
                line(FAIL, f"topic archive: {fname}", "not found")
                all_passed = False

        # Idempotency: re-run publish, file should be byte-for-byte identical
        # (modulo `generated_at` and `updated_at` which include now())
        first_daily = daily.read_text(encoding="utf-8")
        try:
            from digest.obsidian import publish

            publish()
            second_daily = daily.read_text(encoding="utf-8")

            # Strip the timestamp lines for comparison
            def strip_ts(text: str) -> str:
                return "\n".join(
                    line for line in text.splitlines()
                    if not line.startswith("generated_at:")
                    and not line.startswith("updated_at:")
                )

            if strip_ts(first_daily) == strip_ts(second_daily):
                line(PASS, "idempotent re-run (daily)", "byte-equal sans timestamps")
            else:
                line(FAIL, "idempotent re-run (daily)", "content drifted")
                all_passed = False
        except Exception as exc:  # noqa: BLE001
            line(FAIL, "idempotent re-run", f"{type(exc).__name__}: {exc}")
            all_passed = False

        # Run log
        run_log = digest_root / "_meta" / "Run Log.md"
        if run_log.exists() and "published" in run_log.read_text(encoding="utf-8"):
            line(PASS, "run log written")
        else:
            line(FAIL, "run log written")
            all_passed = False

        print()
        if all_passed:
            print("  All Phase 3 checks passed.\n")
            return 0
        print("  Some checks failed; see above.\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
