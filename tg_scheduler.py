"""
tg_scheduler.py — APScheduler setup for the Telegram bot service.

Jobs:
    - drain_queue       : every 15 minutes — processes tg_notification_queue rows
    - deadline_reminders: daily at 09:00 HKT — 24h deadline reminders for unvoted members
    - activity_warnings : every Friday at 17:00 HKT — low-participation alerts
"""

import json
import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
HKT = ZoneInfo("Asia/Hong_Kong")


def setup_scheduler(app) -> AsyncIOScheduler:
    """
    Create, configure, and return an AsyncIOScheduler.
    Call scheduler.start() after this returns.

    Parameters
    ----------
    app : telegram.ext.Application
        The running PTB Application. Provides app.bot and app.bot_data["db_pool"].
    """
    scheduler = AsyncIOScheduler(timezone=HKT)

    scheduler.add_job(
        _drain_queue,
        "interval",
        minutes=15,
        args=[app],
        id="drain_queue",
        replace_existing=True,
    )

    scheduler.add_job(
        _deadline_reminders,
        CronTrigger(hour=9, minute=0, timezone=HKT),
        args=[app],
        id="deadline_reminders",
        replace_existing=True,
    )

    scheduler.add_job(
        _activity_warnings,
        CronTrigger(day_of_week="fri", hour=17, minute=0, timezone=HKT),
        args=[app],
        id="activity_warnings",
        replace_existing=True,
    )

    return scheduler


# ---------------------------------------------------------------------------
# Job implementations
# ---------------------------------------------------------------------------

async def _drain_queue(app) -> None:
    """Fetch unprocessed rows from tg_notification_queue and dispatch them."""
    from tg_notifications import (
        notify_new_topic_vote,
        notify_new_depose_vote,
        notify_vote_result,
    )

    pool = app.bot_data["db_pool"]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, noti_type, payload FROM tg_notification_queue "
            "WHERE processed = FALSE ORDER BY created_at ASC LIMIT 50"
        )

    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)

        try:
            if row["noti_type"] == "new_topic":
                await notify_new_topic_vote(app.bot, pool, **payload)
            elif row["noti_type"] == "new_depose":
                await notify_new_depose_vote(app.bot, pool, **payload)
            elif row["noti_type"] == "vote_result":
                await notify_vote_result(app.bot, pool, **payload)
            else:
                logger.warning("Unknown noti_type '%s' (id=%d)", row["noti_type"], row["id"])
        except Exception as exc:
            logger.error("Failed to process queue row id=%d: %s", row["id"], exc)
            continue

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE tg_notification_queue SET processed = TRUE WHERE id = $1",
                row["id"],
            )

    if rows:
        logger.info("Drained %d notification(s) from queue.", len(rows))


async def _deadline_reminders(app) -> None:
    from tg_notifications import send_deadline_reminders

    pool = app.bot_data["db_pool"]
    try:
        await send_deadline_reminders(app.bot, pool)
    except Exception as exc:
        logger.error("Error in deadline_reminders job: %s", exc)


async def _activity_warnings(app) -> None:
    from tg_notifications import send_activity_warnings

    pool = app.bot_data["db_pool"]
    try:
        await send_activity_warnings(app.bot, pool)
    except Exception as exc:
        logger.error("Error in activity_warnings job: %s", exc)
