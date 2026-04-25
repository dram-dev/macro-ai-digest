# Macro + AI Digest

Twice-daily curated digest covering Fed/markets, China, AI research + downstream business impact, AI capex, and data-viz ideas. Ingested from Economist newsletters, Reddit, high-signal RSS, SEC EDGAR, FRED, and Hacker News.

See `Plan.md` (in Obsidian vault at `80 Digest/_meta/Plan.md`) for full architecture and design decisions.

## Phase 1 scope

This scaffold covers ingestion only — raw items land in SQLite. Triage, summarization, and Obsidian output come in Phases 2–4.

## Prerequisites

- Python 3.12+
- `uv` package manager (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Mac mini (or anywhere) for execution
- API credentials (all free):
  - Reddit app (script type)
  - FRED API key
  - Google Cloud OAuth client (for Gmail)
  - SEC EDGAR needs no key, just a valid user-agent

## Getting started

**→ See `CHECKLIST.md` for the full split-responsibility setup walkthrough.**

Short version for those already familiar:

```bash
uv sync
uv run python scripts/setup.py        # interactive credential wizard
uv run python scripts/smoke_test.py   # validate connectivity
uv run digest init-db
uv run digest ingest all
uv run digest stats
```

## Gmail first-run OAuth

First time you run `digest ingest gmail`:

1. Browser opens to Google consent screen
2. Authorize read-only Gmail access
3. Token cached to `secrets/gmail_token.json`
4. Subsequent runs use the cached token silently

The OAuth scope is `gmail.readonly` — the script cannot modify or delete mail.

## Gmail filter setup (one-time, in Gmail UI)

Create a filter for Economist newsletters:

- **Matches:** `from:(newsletters@economist.com OR noreply@economist.com OR newsletter@e.economist.com)`
- **Action:** Apply label `Digest/Economist`

The ingestor pulls only messages with that label.

## Scheduling (Phase 4, not yet wired)

Production schedule will use `launchd` on the Mac mini:
- 06:30 — full AM run
- 17:30 — delta PM run

For Phase 1, run manually to validate ingestion.

## Project layout

```
macro-ai-digest/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore
├── src/digest/
│   ├── cli.py              # entry point
│   ├── config.py           # pydantic-settings env loader
│   ├── db.py               # SQLite schema + helpers
│   └── ingest/
│       ├── base.py         # IngestedItem + IngestorBase
│       ├── gmail.py
│       ├── reddit.py
│       ├── rss.py
│       ├── edgar.py
│       ├── fred.py
│       └── hackernews.py
├── config/
│   ├── rss_feeds.yaml
│   ├── subreddits.yaml
│   ├── fred_series.yaml
│   └── edgar_tickers.yaml
└── scripts/
    └── init_db.py
```

## Next phases

- **Phase 2:** Ollama + Qwen 2.5 14B triage; Claude Code CLI summarization with `SUMMARIZER_BACKEND` abstraction
- **Phase 3:** Obsidian markdown writer with idempotent upserts
- **Phase 4:** launchd scheduling, Pushover alerts, run log populated

See `Plan.md` for details.
