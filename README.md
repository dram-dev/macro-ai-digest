# Macro + AI Digest

Twice-daily curated digest covering Fed / markets, China, AI research +
downstream business impact, AI capex, and data-viz ideas. Ingested from
Economist newsletters (via Gmail), Reddit, high-signal RSS, Substack, SEC
EDGAR, FRED, Hacker News, arXiv, Hugging Face, CBOE/CFTC/FTD/insider
positioning, Yahoo Finance, and an economic event calendar.

Sibling project: [pc-insurance-digest](https://github.com/dram-dev/pc-insurance-digest)
— shares the MLX server, Ollama, and the same Obsidian vault.

Full architecture lives in `Plan.md` in the Obsidian vault at
`80 Digest/_meta/Plan.md`.

## Status — all phases shipped

**Pipeline:** `ingest → triage (Ollama Qwen2.5:14b) → summarize (MLX
Qwen3.6-27B local) → score → publish (Obsidian) → signals / essay / debate /
dashboard / backtest`

**Ingestors (live):**
- **Gmail / Economist newsletters** — OAuth, label-scoped, read-only
- **Reddit, Substack, Hacker News** — high-signal subreddits + Substack feeds
  + HN ≥100 points
- **EDGAR + insider tx + FTD** — named ticker universe, 8-K / 10-Q / 10-K body
  fetch, Form 4 insider transactions, fails-to-deliver
- **FRED** — macro series with ±1.5σ anomaly gate
- **arXiv + Hugging Face** — AI research papers + trending models
- **RSS** — high-signal financial / AI / China feeds + Google News proxies
- **CBOE / CFTC** — vol surface + COT positioning
- **Yahoo Finance** — price data feeder for stock tracker
- **Calendar** — economic event calendar (CPI, FOMC, NFP, etc.)
- **Clipped** — web clippings via the Clipped service

**Triage / summarize / score:**
- Multi-topic taxonomy with sub-tags
- Hybrid auto-keep — Python enforces material categories (insider buys,
  EDGAR filings from the tracked universe, FRED anomalies, calendar events,
  high-magnitude moves); model handles the rest
- Macro regime detector — risk-on / risk-off / transition; multiplier feeds
  the leaderboard
- Signal leaderboard — multi-factor scoring with topic / source priority,
  recency, LLM materiality; rolling 90-day leaders window, routine insider
  10b5-1 drips collapsed into single summary rows
- Sentiment classifier (MLX-local financial sentiment model)
- Entity extraction + ticker linkage — items get attached to tickers in the
  stock tracker
- Outcomes tracking — post-hoc validation of past signal calls
- Cluster + narrative velocity — week-over-week momentum across clusters,
  with cached Claude-assigned display names instead of raw TF-IDF labels
- Storyline threading — persistent multi-day narratives (Claude-maintained
  running state + daily deltas); movers surface in the Brief, weekly themes
  are seeded from active storylines
- Prediction scorecard — falsifiable calls extracted from essays / debate
  syntheses / weekly contrarian signals, judged at their horizon against the
  digest's own evidence (correct / incorrect / unclear with grace window)

**Publish (Obsidian vault, `80 Digest/`):**
- `Brief/YYYY-MM-DD Brief.md` — mobile-first front page: regime, top 5
  signals (with seeded Claude chat links), connection threads, outcome
  scoreboard, upcoming events
- `Daily/YYYY-MM-DD.md` — full daily note with regime callout, top signals,
  per-topic summaries (long-tail items carry plain `#id` refs to stay light)
- `Topics/<Topic>.md` — per-topic archive, idempotent upsert by item ID, with
  a weekly Claude-maintained "State of play" brief on top (standing thesis,
  what changed, what to watch)
- `Storylines/<Name>.md` — one page per tracked narrative (current state +
  newest-first timeline), plus a status-grouped index
- `Weekly/<YYYY-WW>.md` — themes, must-reads, contrarian signal, weekly essay;
  the full item replay lives in the companion `<YYYY-WW> Items.md`
- `Investments/<TICKER>.md` — stock tracker notes with signal overlays
- `_meta/Run Log.md` — append-only operations log
- `Signal/Scorecard.md` — prediction hit rate by source, open calls, resolved
  log; right/wrong retro also lands in the weekly + Brief scoreboard
- HTML dashboard with cross-asset correlation + upcoming events overlay
- Bull / bear / synthesis debate on the week's contested theses
- Backtest report — source × topic outcome analysis

## Schedule

Production schedule on the Mac mini (launchd jobs in `launchd/`):

| Job | When |
|---|---|
| `am` pipeline | daily 01:00 |
| `pm` pipeline | daily 13:00 |
| `calendar` | Fri 20:45 |
| `weekly` | Fri 19:00 |
| `signals` | Fri 21:00 |
| `velocity` | Fri 21:15 |
| `backtest` | Fri 21:20 |
| `essay` | Fri 21:30 |
| `debate` | Fri 22:00 |
| `dashboard` | Fri 22:15 |

Staggered with [pc-insurance-digest](https://github.com/dram-dev/pc-insurance-digest)
(am 04:00, pm 16:00, weekly Sat 06:00) so the shared MLX server never has two
clients in flight at once.

## Prerequisites

- Python 3.12+
- `uv` (`brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Mac mini (or any Apple Silicon Mac) for execution
- Ollama running locally with `qwen2.5:14b` pulled (triage). Optional but
  recommended: `ollama pull nomic-embed-text` — enables embedding-based
  clustering; without it, clustering falls back to TF-IDF automatically.
- MLX-LM server (this project's `com.dr.mlx.server` launchd job keeps it up;
  pc-insurance-digest depends on it too)
- Free API credentials:
  - Reddit script app (optional — public JSON endpoint works without it)
  - FRED API key
  - Google Cloud OAuth client (for Gmail read-only)
  - SEC EDGAR — no key, just a real-name + email user-agent
  - Optional fallback summarizer keys: `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`

## Getting started

**→ See `CHECKLIST.md` for the full split-responsibility setup walkthrough.**

Short version:

```bash
uv sync
uv run python scripts/setup.py        # interactive credential wizard
uv run python scripts/smoke_test.py   # validate connectivity
uv run digest init-db
uv run digest ingest all
uv run digest sources                 # live catalog: every source + 7-day pulse
uv run digest pipeline --run-type manual
uv run digest stats
```

CLI commands: `ingest`, `sources`, `triage`, `summarize`, `pipeline`, `publish`,
`weekly`, `regime`, `ensemble`, `outcomes`, `cluster`, `storylines`,
`predictions`, `topic-state`, `signals`, `essay`, `debate`, `dashboard`,
`sentiment`, `entities`, `stocks`, `calendar`, `velocity`, `backtest`,
`notify`, `ask`, `ask-bot`, `recent`, `stats`, `health`, `security`, `init-db`.

## Telegram push notifications (optional)

High-signal items (triage score ≥ `NOTIFY_MIN_SCORE`, default 0.80) can fire a
terse Telegram push at the end of each pipeline run — deduped per item so the
am/pm runs never double-fire. Output is otherwise file-only (Obsidian).

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. Message your new bot once, then read your chat id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates` (or [@userinfobot](https://t.me/userinfobot)).
3. Set `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` in `.env`.
4. Verify: `uv run digest notify --test` (a banner should land on your phone).

## Ask the archive (local RAG)

Query the kept-item corpus in natural language — retrieval over local Ollama
embeddings (`nomic-embed-text`) + answer synthesis through the configured
summarizer backend (MLX-local by default), with `[n]` citations.

```bash
uv run digest ask "What's the latest on hyperscaler AI capex?"
uv run digest ask "Any signals on the 2s10s spread?" --days 30 -k 10
```

The first run embeds the corpus once (cached in `item_embeddings`); later runs
only embed newly-summarized items.

### Ask from Telegram

`digest ask-bot` runs a long-polling listener (no public endpoint needed) that
answers questions sent to your bot, replying with the synthesized answer +
sources. It responds **only** to your configured `TELEGRAM_CHAT_ID`. Run it as a
daemon via the bundled `com.dr.digest.askbot` launchd job (KeepAlive), or
foreground for a quick try:

```bash
uv run digest ask-bot   # then message your bot from your phone
```

Set `NOTIFY_BRIEF_PING=true` for an extra once-per-run "Brief ready" ping with
an `obsidian://` deep link. Leave the token blank to disable entirely.

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

## Scheduling

```bash
bash scripts/install_launchd.sh
launchctl list | grep -E '(com\.dr\.digest|com\.dr\.mlx)'
```

## Project layout

```
macro-ai-digest/
├── pyproject.toml
├── README.md
├── CHECKLIST.md
├── .env.example
├── launchd/                       # am / pm + Fri-night batch + mlx server
├── scripts/                       # setup wizard, smoke test, install_launchd
├── config/
│   ├── rss_feeds.yaml
│   ├── subreddits.yaml
│   ├── substack_feeds.yaml
│   ├── fred_series.yaml
│   └── edgar_tickers.yaml
└── src/digest/
    ├── cli.py                     # Click entry points
    ├── config.py                  # pydantic-settings env loader
    ├── db.py                      # SQLite schema + helpers (shared with PC Digest)
    ├── triage.py                  # Ollama Qwen2.5 prompt + auto-keep hooks
    ├── summarize.py               # MLX runner + materiality prompt
    ├── obsidian.py                # daily / weekly / topic-archive writer
    ├── weekly.py                  # weekly synthesis (themes / must-reads / contrarian)
    ├── essay.py                   # long-form weekly essay
    ├── debate.py                  # bull / bear / synthesis debate
    ├── dashboard.py               # HTML dashboard (correlations + calendar overlay)
    ├── backtest.py                # source × topic outcome analysis
    ├── signals.py                 # leaderboard scoring
    ├── ensemble.py                # multi-model signal fusion
    ├── outcomes.py                # post-hoc validation of past signals
    ├── cluster.py                 # narrative clustering
    ├── velocity.py                # week-over-week cluster momentum
    ├── sentiment.py               # MLX-local financial sentiment classifier
    ├── entities.py                # entity extraction + ticker linkage
    ├── stock_tracker.py           # per-ticker Investments/ folder
    ├── macro_regime.py            # risk-on / risk-off / transition detector
    ├── indicators.py              # macro indicators bundle
    ├── connections.py             # cross-asset / cross-topic links
    ├── charts.py                  # native Mermaid xychart-beta blocks
    ├── viz.py / health.py / security.py
    └── ingest/
        ├── base.py
        ├── gmail.py, reddit.py, substack.py, hackernews.py, rss.py
        ├── edgar.py, insider.py, ftd.py
        ├── fred.py, calendar.py
        ├── arxiv.py, huggingface.py
        ├── cboe.py, cftc.py, yahoo.py
        └── clipped.py
```

## Sibling project

[pc-insurance-digest](https://github.com/dram-dev/pc-insurance-digest) is the
P&C-insurance counterpart and the canonical home of the shared **`digest-core`**
framework (`packages/digest-core/`). This repo now **runs on that core**,
consuming it as an editable path dep — the SQLite base + CRUD, `IngestorBase` +
the ingestor registry, the summarizer backends/runner, and the `digest sources`
catalog all come from `digest_core`; macro keeps its domain logic (regime,
essays, debate, velocity, clustering, dashboard, ~15 ingestors) on top. Adding a
source is *drop a file in `digest/ingest/`, subclass `IngestorBase`, give it a
`name`* — it self-registers and shows up in `digest sources`. The remaining
design seams are tracked in the PC repo's
`packages/digest-core/SEAMS_PLAN.md`.
