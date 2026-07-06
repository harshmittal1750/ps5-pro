# PS5 Tweet Watcher

This watcher polls `@ICGOriginal` through Nitter every 2-5 seconds, remembers the last seen tweet ID, and alerts when a new tweet matches PS5 restock rules.

## What it does

1. Fetches the latest tweets from Nitter.
2. Compares the newest tweet ID with the last stored ID.
3. Checks new tweets against keyword rules.
4. Triggers a desktop notification.
5. Optionally triggers a phone call through Twilio.

## Files

- `monitor.py`: watcher script
- `.env.example`: configuration template
- `.state/icg_last_seen.json`: runtime state created automatically

## Setup

```bash
cp .env.example .env
python3 monitor.py --once
```

The first run primes the stored tweet ID so you do not get spammed by older tweets. After that, start the loop:

```bash
python3 monitor.py
```

## Configuration

The script reads `.env` automatically.

- `NITTER_BASE_URL`: Nitter instance to use, for example `https://nitter.net`
- `NITTER_USERNAME`: account to watch, default `ICGOriginal`
- `POLL_MIN_SECONDS` and `POLL_MAX_SECONDS`: random delay range between polls
- `KEYWORD_RULES`: semicolon-separated rules, where each rule is a comma-separated list of required terms
- `MACOS_NOTIFICATIONS`: `true` enables local macOS notifications through `osascript`
- `ALERT_ON_STARTUP`: `true` lets the first run alert on the current latest tweet

Example `KEYWORD_RULES`:

```env
KEYWORD_RULES=ps5,amazon;ps5,restock;ps5,flash sale;ps5,slim digital
```

This means a tweet matches if it contains all terms from any one rule.

## Phone call alerts

To enable phone calls, fill in these fields in `.env`:

```env
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+1...
ALERT_TO_NUMBER=+91...
TWILIO_VOICE_MESSAGE=PS5 restock alert from I C G. Check the latest tweet now.
```

If those four required Twilio fields are present, the script places a phone call whenever a matching tweet appears.

## Notes

- Public Nitter instances can be unreliable. If one stops responding, switch `NITTER_BASE_URL`.
- The script uses RSS first and falls back to parsing the HTML timeline.
- State is stored locally so restarts do not repeat old alerts.

## Railway

This works better on Railway as a worker/background service than as a web app.

- Start command: `python3 monitor.py`
- Add your config as Railway environment variables instead of relying on a local `.env` file.
- `MACOS_NOTIFICATIONS` should be `false` on Railway because Railway runs Linux, not macOS.
- If you want phone calls, set the Twilio variables in Railway:
  - `TWILIO_ACCOUNT_SID`
  - `TWILIO_AUTH_TOKEN`
  - `TWILIO_FROM_NUMBER`
  - `ALERT_TO_NUMBER`
- The default `.state/icg_last_seen.json` file is not durable across redeploys/restarts unless you attach a persistent volume.
- Best options for state on Railway:
  - Mount a persistent volume and point `STATE_FILE` to something on that volume.
  - Or replace file state with Redis/Postgres later if you want stronger persistence.
- First deploy tip: keep `ALERT_ON_STARTUP=false` so the service just primes the latest tweet and waits for the next one.

## Deployment Files

This repo now includes explicit deployment metadata so builders do not have to infer the runtime from a single Python file.

- `requirements.txt`: marks this as a Python project for Nixpacks/Railpack
- `nixpacks.toml`: explicit Nixpacks setup and start command
- `Dockerfile`: explicit container build
- `.dockerignore`: trims local-only files from Docker builds
- `railway.json`: tells Railway how to start the worker

## Railway Checklist

1. Create a Railway service from this repo.
2. Keep it as a worker/background service.
3. Set these environment variables:
   - `NITTER_BASE_URL`
   - `NITTER_USERNAME`
   - `POLL_MIN_SECONDS`
   - `POLL_MAX_SECONDS`
   - `KEYWORD_RULES`
   - `ALERT_ON_STARTUP=false`
   - `MACOS_NOTIFICATIONS=false`
4. If using Twilio, also set:
   - `TWILIO_ACCOUNT_SID`
   - `TWILIO_AUTH_TOKEN`
   - `TWILIO_FROM_NUMBER`
   - `ALERT_TO_NUMBER`
5. If you want persistent last-seen tweet tracking across restarts, attach a volume and set `STATE_FILE` to a mounted path like `/data/icg_last_seen.json`.
