# secrets/

This directory holds OAuth credentials and tokens. Everything inside is
**gitignored** — never commit.

## What goes here

### `gmail_credentials.json`  *(you must provide)*

The OAuth client ID downloaded from Google Cloud Console:

1. https://console.cloud.google.com/apis/credentials
2. Create project → enable Gmail API → OAuth consent screen (External, Test users = yourself)
3. Create Credentials → OAuth client ID → **Desktop app**
4. Download JSON → rename to `gmail_credentials.json` → drop here.

### `gmail_token.json`  *(auto-generated on first run)*

Created automatically by the first `digest ingest gmail` run after you
authorize access in the browser. Subsequent runs refresh it silently.

**From your phone:** send `/gmail` to the digest's Telegram bot. Open the
link it replies with and approve. Your phone then lands on a `localhost:8765`
page that won't load; copy that page's whole address and send it back to the
bot, which saves the token here. While you're signing in, `gmail_pending.json`
holds the sign-in's one-time verifier (0600, gone once it's done).

**It lasts 7 days while the consent screen is in Testing.** Google ends a
Testing app's sign-in after a week; that is why the token written on
2026-06-17 stopped working (`invalid_grant`) and Gmail went quiet until
October. The bot says so when Google reports it. To keep it signed in,
publish the app (Google Auth Platform → Audience → Publish app); a personal
app needs no verification for that, and Google shows an "unverified app"
notice at sign-in instead.

## Revoking access

To fully revoke: delete `gmail_token.json` here, and revoke the client at
https://myaccount.google.com/permissions.

## OAuth scope

Read-only: `https://www.googleapis.com/auth/gmail.readonly`.
This script cannot send, modify, or delete mail.
