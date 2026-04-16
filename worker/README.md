# Cloudflare Telegram Worker

This worker replaces the long-running Python Telegram bot with:

- `POST /telegram/<secret>` for Telegram webhooks
- `GET /health` for smoke checks
- cron triggers for queue draining and reminder jobs

## Bot Behavior

- `/link <code>` uses a one-time code generated from the website account page
- account linking only works in a private Telegram chat
- `/status`, `/pending`, `/myvotes`, and `/unlink` only work for linked private chats
- notification broadcasts target linked committee accounts, not just `active` rows in `accounts`
- a blocked / invalid Telegram chat is auto-unlinked so one bad recipient does not stall the whole queue

## Setup

1. Create a Hyperdrive binding for the existing PostgreSQL database.
2. Disable Hyperdrive query caching for this binding so Telegram reads stay fresh.
3. Update `wrangler.jsonc` with the correct Hyperdrive binding id and app URL.
4. Set secrets:

```bash
cd worker
wrangler secret put BOT_TOKEN
wrangler secret put TELEGRAM_WEBHOOK_SECRET
```

5. Install dependencies and deploy:

```bash
npm install
npm run deploy
```

6. Register Telegram commands and webhook:

```bash
WORKER_BASE_URL="https://<your-worker-url>" \
BOT_TOKEN="<bot-token>" \
TELEGRAM_WEBHOOK_SECRET="<secret>" \
npm run register
```

## Cutover

1. Deploy the worker.
2. Confirm `GET /health` works.
3. Run `npm run register`.
4. Verify webhook commands and cron jobs in staging.
5. Stop the old polling bot after Telegram traffic has switched.
