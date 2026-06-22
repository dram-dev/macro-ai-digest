---
title: Macro + AI Digest — Checklist & Instructions
created: 2026-04-24
updated: 2026-04-27
status: Phases 1-2 complete; Phase 3 code shipped, ready to activate
project_path: /Users/dramsey/Projects/macro-ai-digest
vault_path: /Users/dramsey/Documents/Obsidian Vault/vault_build
---

# Macro + AI Digest — Checklist & Instructions

Split by who-does-what. **[YOU]** is you on the Mac mini or in a browser. **[ME — DONE]** is already in the zip. **[ME — TEMPLATED]** is ready in the zip and activates when you run it.

Legend: `- [x]` done already, `- [ ]` to do.

---

## PHASE 1 — Ingestion (current focus, ~2 hours total)

### Part A. One-time account setup  *(you + links)*

Each credential below is free. Do these in any order; grab all three, then move to Part B.

> **Note on Reddit:** as of late 2025 Reddit requires pre-approval for personal-use API scripts. The default ingestor uses Reddit's public JSON endpoint (no auth, no waiting), so you can skip Reddit setup entirely. If/when your `ticket_form_id=14868593862164` request is approved, see *Part G — Optional: enable PRAW mode* below.

#### A.1  FRED API key  **[YOU]**  ✓ done

- [x] Open https://fred.stlouisfed.org/docs/api/api_key.html
- [x] Sign in or create a free account
- [x] Click **Request API Key**
- [x] Copy the 32-character hex key

#### A.2  SEC EDGAR user-agent  **[YOU]**  *(no account, just format)*  ✓ done

- [x] Decide the string to identify yourself to SEC. Format: `Your Name your.email@example.com`
- [x] SEC uses this to rate-limit and contact heavy users. Real name + real email.

#### A.3  Gmail OAuth client  **[YOU]**  ✓ done

This is the slowest one — ~10 minutes in Google Cloud Console.

- [x] Open https://console.cloud.google.com/
- [x] Create project: `macro-ai-digest`
- [x] In the project, go to **APIs & Services → Library**
- [x] Search **Gmail API**, click **Enable**
- [x] Go to **OAuth consent screen**
  - [x] User type: **External**
  - [x] App name: `macro-ai-digest`
  - [x] User support email: your email
  - [x] Developer contact: your email
  - [x] Scopes: skip (we add at runtime)
  - [x] Test users: **add your own Gmail address**  ← *critical; without this, OAuth will 403*
- [x] Go to **Credentials → Create Credentials → OAuth client ID**
  - [x] Application type: **Desktop app**
  - [x] Name: `macro-ai-digest-desktop`
  - [x] Click **Create**, then **Download JSON**
- [x] Rename the downloaded file to `gmail_credentials.json`

#### A.4  Gmail filter (inside Gmail itself)  **[YOU]**  ✓ done

- [x] Open Gmail in a browser
- [x] Settings (gear) → **See all settings** → **Filters and Blocked Addresses**
- [x] Click **Create a new filter**
- [x] **From:** paste this whole string:
      `newsletters@economist.com OR noreply@economist.com OR newsletter@e.economist.com OR news@e.economist.com`
- [x] Click **Create filter**
- [x] Check **Apply the label:** → New label → `Digest/Economist`
- [x] Check **Also apply filter to matching conversations**
- [x] Click **Create filter**

---

### Part B. Mac mini prep  **[YOU]**  ✓ done

- [x] Install Homebrew (note: required `eval "$(/opt/homebrew/bin/brew shellenv)"` + adding to `~/.zprofile` to put `brew` on PATH)
- [x] `brew install python@3.12 uv git`
- [x] Verify: `python3 --version` (≥3.12) and `uv --version`
- [x] Pick a project home — actual path: **`/Users/dramsey/Projects/macro-ai-digest`**

---

### Part C. Deploy the code  *(mixed)*

#### [ME — DONE]
- [x] Repo scaffolded and zipped: `macro-ai-digest.zip`
- [x] 6 ingestors, SQLite schema, CLI, YAML configs — all validated
- [x] `setup.py` wizard for credential entry
- [x] `smoke_test.py` for end-to-end connectivity check
- [x] launchd plists for AM/PM scheduling (Phase 4, pre-staged)
- [x] `install_launchd.sh` helper to wire launchd with one command
- [x] `secrets/README.md` documenting what goes where

#### [YOU]  ✓ done

- [x] Transferred `macro-ai-digest.zip` to Mac mini
- [x] Unzipped to `/Users/dramsey/Projects/macro-ai-digest`
- [x] `cd /Users/dramsey/Projects/macro-ai-digest`
- [x] `uv sync`  *(installed all Python deps into a project-local `.venv`)*
- [x] Dropped `gmail_credentials.json` from **A.3** into `./secrets/`

---

### Part D. Configure credentials  *(mixed)*

#### [ME — TEMPLATED]
- [x] `.env.example` with every variable documented
- [x] `scripts/setup.py` — interactive wizard that validates and writes `.env` at mode 0600

#### [YOU]

- [ ] `uv run python scripts/setup.py`
- [ ] Paste your FRED API key
- [ ] Paste your EDGAR user-agent string
- [ ] When asked about Reddit, accept the default (skip — uses public JSON)
- [ ] The wizard writes `.env` and reminds you about the Gmail filter

The wizard is re-runnable. Values already in `.env` show as defaults; hit Enter to keep.

---

### Part E. Validate everything  *(mixed)*

#### [ME — TEMPLATED]
- [x] `scripts/smoke_test.py` — one minimal call per source, reports pass/fail per line

#### [YOU]

- [ ] `uv run python scripts/smoke_test.py`
- [ ] Expect: `6/6 passed` (gmail will show ⚠ until you do F.1)
- [ ] If anything is red: re-run `setup.py`, or fix the specific credential manually in `.env`

---

### Part F. First real ingest  **[YOU]**

- [ ] `uv run digest init-db`
- [ ] `uv run digest ingest gmail`
  - First run **opens a browser** for OAuth consent
  - Sign in, authorize read-only Gmail access
  - Token cached to `secrets/gmail_token.json`; all future runs silent
  - ⚠ If you're SSH'd in without a display, do this once directly on the mini (Jump Desktop or VS Code Server with forwarded browser)
- [ ] `uv run digest ingest rss`
- [ ] `uv run digest ingest reddit`
- [ ] `uv run digest ingest fred`
- [ ] `uv run digest ingest edgar`
- [ ] `uv run digest ingest hn`
- [ ] `uv run digest stats` — confirm non-zero items for each source
- [ ] `uv run digest recent --source rss --limit 5` — eyeball a few titles

#### Success criteria

- [ ] `digest stats` shows items across all six sources
- [ ] `digest recent` titles look relevant to your topics
- [ ] No stack traces in output
- [ ] `data/state.db` exists and grows between runs

Expected Phase 1 elapsed time once credentials are in hand: **10–15 min**.

---

### Part G. Optional — enable PRAW mode when Reddit approves you  **[YOU, future]**

If your Reddit data API ticket gets approved (form `ticket_form_id=14868593862164`), the JSON-mode default still works fine — but PRAW gets you a tiny bit more (richer error messages, structured responses, official rate limits). To switch:

- [ ] Re-run `uv run python scripts/setup.py`
- [ ] Answer **N** to "Skip Reddit credentials?"
- [ ] Paste client ID, secret, and your Reddit username
- [ ] Wizard sets `REDDIT_USE_PRAW=true` automatically
- [ ] `uv run python scripts/smoke_test.py` will now exercise the PRAW path
- [ ] Done — no other code changes needed

To roll back to JSON mode at any point: edit `.env` and set `REDDIT_USE_PRAW=false`.

---

## PHASE 2 — Triage + Summarization  *(active — code shipped)*

### What it does
Local Qwen 14B reads every newly-ingested item and decides keep-or-drop with a topic and score. The top-N items that survived (default 20, capped) are then summarized by the **local MLX server** (`mlx_local`, the default backend) for the rich "summary + why it matters + confidence + see-also" treatment. The backend is pluggable — `claude_cli_pro`, `haiku_api`, `gemini_flash_free`, and `local_qwen` are drop-in alternatives via `SUMMARIZER_BACKEND`. Results land in the same SQLite items table; Phase 3 will write them to Obsidian.

### [ME — DONE]
- [x] `src/digest/db.py` — Phase 2 schema migrations (idempotent, validated)
- [x] `src/digest/triage.py` — Ollama HTTP client, JSON-mode prompts, decision normalizer
- [x] `src/digest/summarize.py` — backend-abstracted summarizer with 5 backends:
      `mlx_local` (default), `claude_cli_pro`, `haiku_api`, `gemini_flash_free`, `local_qwen`
- [x] `src/digest/cli.py` — new commands: `digest triage`, `digest summarize`, `digest pipeline`
- [x] `digest stats` extended with triage status and 7-day summarizer activity
- [x] `summarizer_log` table records duration, char counts, status per call
- [x] `scripts/smoke_test_phase2.py` — validates Ollama + Qwen + Claude CLI before a real run
- [x] `.env.example` updated with all Phase 2 settings + budgets

### [YOU]

**Prerequisites — already done:**
- [x] Phase 1 ingestion working end-to-end
- [x] Ollama installed and `qwen2.5:14b` pulled
- [x] Claude Code installed and authenticated (Pro account)

**Activation steps:**

- [ ] `cd /Users/dramsey/Projects/macro-ai-digest`
- [ ] Replace your project directory contents with the new zip (the migrations make this safe — your existing items are preserved)
- [ ] `uv sync` *(no new top-level deps; just refreshes)*
- [ ] `uv run digest init-db` — applies the Phase 2 migrations to your existing DB
- [ ] `uv run python scripts/smoke_test_phase2.py` — should hit `6/6 passed`
- [ ] `uv run digest triage --limit 50` — first triage on a small batch to watch it work
- [ ] `uv run digest stats` — confirm `triage_decision` counts (kept/dropped/pending)
- [ ] `uv run digest summarize --limit 3` — first summarization on just 3 items, watch closely
- [ ] Inspect results: `sqlite3 data/state.db "SELECT id, topic, confidence, summary FROM items WHERE summary IS NOT NULL LIMIT 3"`
- [ ] If those 3 look good, run `uv run digest pipeline` for the full ingest → triage → summarize cycle

### Cost & rate-limit kill switch

Every summarizer call writes to `summarizer_log` with input/output character counts. After a few real runs:

- [ ] Check usage: `uv run digest stats` — bottom table shows backend activity over the last 7 days
- [ ] If you switch to `claude_cli_pro` and Pro rate limits collide with your interactive Claude work: open `.env`, set `SUMMARIZER_BACKEND=haiku_api`, add `ANTHROPIC_API_KEY`, set a $5/mo budget cap in Anthropic Console. No code changes.
- [ ] Backend ladder (default first): `mlx_local` → `claude_cli_pro` → `haiku_api` → `gemini_flash_free` → `local_qwen`. All share the exact same prompts and output schema; flip in `.env`.

### Tunables in `.env`

```
SUMMARIZER_BACKEND=mlx_local          # local MLX (default); cloud backends are the kill-switch
SUMMARIZER_MODEL=sonnet               # used by cloud backends: sonnet | opus | haiku
SUMMARIZER_MAX_PER_RUN=20             # hard cap per run
SUMMARIZER_MAX_PER_SOURCE=15          # per-source cap within a run
SUMMARIZER_TIMEOUT_SEC=120            # per-item ceiling
OLLAMA_MODEL=qwen2.5:14b              # try qwen2.5:32b if 14B underdelivers
TRIAGE_MIN_SCORE=0.5                  # raise to be more selective
```

### Success criteria

- [ ] Triage runs at ~2-5 sec per item on M4 Pro (faster on warm cache)
- [ ] `keep` rate is in the 15-30% range — too high = triage prompt too lenient, too low = your interests defined too narrowly
- [ ] Summarized items have specific, useful "why it matters" text — not generic filler
- [ ] No 429 collisions during your normal interactive Claude work

---

## PHASE 3 — Obsidian Writer  *(active — code shipped)*

### What it does
Reads summarized + kept items from SQLite and writes them to your Obsidian vault as Markdown. Daily notes are date-keyed and rewritten on each run (idempotent). Topic archives accumulate newest-on-top with a YAML index of all entries, using HTML-comment markers so re-runs upsert by item ID instead of duplicating.

### [ME — DONE]
- [x] `src/digest/obsidian.py` — Paths resolver, daily-note writer, topic archive writer
- [x] DB migration adds `obsidian_written_at` column + index (idempotent)
- [x] Topic-slug → display-label mapping (e.g. `fed_markets` → "Fed & Markets")
- [x] Daily note: YAML frontmatter, sectioned by topic in canonical order, kept-unsummarized at bottom
- [x] Topic archives: YAML index block + ID-marked entries, newest first
- [x] Confidence badges (🟢 high / 🟡 medium / 🟠 low) and see-also rendered inline
- [x] Wikilinks from daily → topic archives for graph navigation
- [x] Run Log appended with each publish (in `_meta/Run Log.md`)
- [x] CLI: `digest publish` and `digest publish --topics-only`
- [x] CLI: `digest pipeline` extended with publish stage 4
- [x] `scripts/smoke_test_phase3.py` validates against a temp vault — 14/14 checks passed in dev

### [YOU]

- [ ] `cd /Users/dramsey/Projects/macro-ai-digest`
- [ ] Replace project contents with the new zip (DB migrations preserve existing data)
- [ ] `uv sync`
- [ ] `uv run digest init-db` — applies the new migration
- [ ] Verify `OBSIDIAN_VAULT_PATH` in `.env` is set to `/Users/dramsey/Documents/Obsidian Vault/vault_build` (already in `.env.example`)
- [ ] `uv run python scripts/smoke_test_phase3.py` — exercises the writer against a temp vault, must pass before touching your real one
- [ ] `uv run digest publish` — first real write to the vault
- [ ] Open Obsidian, navigate to `80 Digest/Daily/<today>.md` — confirm it renders cleanly
- [ ] Check `80 Digest/Topics/` — should see topic archives for every topic with summaries
- [ ] Check graph view — daily note should link to topic archives via wikilinks

### Layout you'll see in your vault

```
/Users/dramsey/Documents/Obsidian Vault/vault_build/
└── 80 Digest/
    ├── Daily/
    │   └── 2026-04-27.md          ← today's digest, regenerated each pipeline run
    ├── Topics/
    │   ├── Fed & Markets.md       ← newest-on-top, with YAML index
    │   ├── China.md
    │   ├── AI Thinkers.md
    │   ├── AI Capex.md
    │   ├── AI Business Apps.md
    │   ├── AI & Semis.md
    │   └── Data Viz Ideas.md
    └── _meta/
        └── Run Log.md             ← append-only ops log
```

### Re-running safely

Daily notes are byte-equivalent on identical input (sans `generated_at` timestamp). Topic archives use `<!-- digest:item:<id>:begin -->` markers so the same item written twice produces the same archive — no dupes, no drift. You can run `digest pipeline` as many times per day as you want.

To regenerate a past day: `uv run digest publish --date 2026-04-26`
To rebuild only topic archives without touching daily: `uv run digest publish --topics-only`

### Success criteria

- [ ] Phase 3 smoke test passes 14/14 against the temp vault
- [ ] `digest publish` produces a daily note that renders cleanly in Obsidian
- [ ] Wikilinks from daily → topic archives navigate correctly in graph view
- [ ] Re-running `digest publish` doesn't change the file content
- [ ] Topic archives accumulate over days without duplicating any entry

---

## PHASE 4 — Scheduling  *(pre-staged, activates now that Phases 2-3 are ready)*

#### [ME — DONE]
- [x] `launchd/com.dr.digest.am.plist` template (6:30 AM)
- [x] `launchd/com.dr.digest.pm.plist` template (5:30 PM)
- [x] `scripts/install_launchd.sh` — substitutes paths and loads jobs

#### [YOU]  *(when you're ready)*

- [ ] `bash scripts/install_launchd.sh`
- [ ] `launchctl list | grep com.dr.digest` — confirm both jobs loaded
- [ ] Wait for 6:30 AM the next morning; check `logs/am.out.log`

Stop anytime with:
```
launchctl unload ~/Library/LaunchAgents/com.dr.digest.am.plist
launchctl unload ~/Library/LaunchAgents/com.dr.digest.pm.plist
```

---

## Quick reference — commands you'll actually use

```bash
# Setup (one time)
uv sync
uv run python scripts/setup.py
uv run python scripts/smoke_test.py
uv run python scripts/smoke_test_phase2.py     # Phase 2 prerequisites
uv run python scripts/smoke_test_phase3.py     # Phase 3 — writes to a temp vault
uv run digest init-db

# Phase 1 — ingestion
uv run digest ingest all
uv run digest ingest reddit                    # or any single source
uv run digest stats
uv run digest recent --source rss --limit 10

# Phase 2 — triage & summarize
uv run digest triage                           # Qwen filters pending items
uv run digest summarize                        # Claude summarizes survivors (cap 20)

# Phase 3 — publish to Obsidian
uv run digest publish                          # writes today's daily + all topic archives
uv run digest publish --date 2026-04-26        # rebuild a past day
uv run digest publish --topics-only            # refresh topic archives only

# Full pipeline
uv run digest pipeline                         # ingest + triage + summarize + publish

# Phase 4 scheduling
bash scripts/install_launchd.sh
```

## If something breaks

- **Smoke test fails on one source** → re-run `setup.py`, or open `.env` and check that credential line
- **Gmail OAuth loops / 403** → make sure your email is added as a Test user on the OAuth consent screen (A.3)
- **Reddit 401** → the client ID is the short 14-char string, not the app name
- **EDGAR 403** → your user-agent string needs a real email; SEC blocks generic ones
- **FRED returns empty** → some series update weekly/monthly; not a bug
- **RSS warnings for specific feeds** → feed URLs drift; edit `config/rss_feeds.yaml`, redeploy

---

*Last updated 2026-04-24. Living document — append notes as Phase 1 completes.*
