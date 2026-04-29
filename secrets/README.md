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

## Revoking access

To fully revoke: delete `gmail_token.json` here, and revoke the client at
https://myaccount.google.com/permissions.

## OAuth scope

Read-only: `https://www.googleapis.com/auth/gmail.readonly`.
This script cannot send, modify, or delete mail.
