# Telegram Worker Operations

This file covers the day-to-day operational commands for the Cloudflare Telegram Worker.

## Working Directory

Run all Worker commands from:

```bash
cd /Users/lzlovecats/Documents/GitHub/skhlmc_dbt_marksys/worker
```

## Health Check

Check that the deployed Worker is reachable:

```bash
curl https://skhlmc-telegram-worker.lzlovecats.workers.dev/health
```

Expected response:

```json
{"ok":true}
```

## Deploying Changes

After changing Worker code:

```bash
npm run typecheck
npm test
npm run deploy
```

## Viewing Logs

Tail runtime logs:

```bash
npx wrangler tail
```

Use this when:

- Telegram commands are not replying
- cron jobs are failing
- queue rows are not being processed

## Managing Secrets

Update secrets if the bot token or webhook secret changes:

```bash
npx wrangler secret put BOT_TOKEN
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET
```

After changing either one, re-register the webhook.

## Re-registering Telegram Webhook

Run this if:

- the Worker URL changes
- the bot token changes
- the webhook secret changes
- Telegram commands need to be refreshed

```bash
BOT_TOKEN='YOUR_BOT_TOKEN' \
WORKER_BASE_URL='https://skhlmc-telegram-worker.lzlovecats.workers.dev' \
TELEGRAM_WEBHOOK_SECRET='YOUR_WEBHOOK_SECRET' \
npm run register
```

## Hyperdrive and Config

Main config file:

- [wrangler.jsonc](/Users/lzlovecats/Documents/GitHub/skhlmc_dbt_marksys.worktrees/codex-workspace/worker/wrangler.jsonc)

Check these values before deploy:

- `APP_URL`
- Hyperdrive binding id
- cron schedules

If the PostgreSQL connection changes, update the Hyperdrive configuration in Cloudflare and keep the binding id in `wrangler.jsonc` in sync.

## Queue Troubleshooting

The queue table is `telegram_notification_queue`.

Important fields:

- `is_processed`: row finished successfully
- `processing_token`: row is currently claimed by a Worker run
- `processing_started_at`: when the claim started
- `last_error_message`: most recent processing error

Useful query:

```sql
SELECT id, notification_type, created_at, is_processed, processing_token, processing_started_at, last_error_message
FROM telegram_notification_queue
ORDER BY created_at DESC
LIMIT 50;
```

If rows are stuck:

1. Check `npx wrangler tail`
2. Check `last_error_message`
3. Confirm Telegram secrets are valid
4. Confirm Hyperdrive still points to the correct database

## Smoke Test Checklist

After deploy:

1. `curl /health`
2. Send `/help` to the bot
3. Send `/status` to the bot
4. Trigger a new queue event from the app and confirm delivery

## Rollback

If a deploy breaks production:

1. Re-deploy the previous known-good Worker version from git history
2. Re-run `npm run register` only if the webhook secret or URL changed
3. Use `npx wrangler tail` to confirm recovery

The old Python polling bot should stay off during normal Worker operation.
