"""
tg_notifications.py — Telegram notification functions for the SKH LMC Debate Marking System.

All functions are async and use an asyncpg connection pool.
They are called by the bot scheduler (tg_scheduler.py) to send push notifications.

Notification types:
    1. notify_new_topic_vote   — new topic proposal opened for voting
    2. notify_new_depose_vote  — new deposition motion opened for voting
    3. notify_vote_result      — a vote resolved (passed / rejected)
    4. send_deadline_reminders — 24h reminder for members who haven't voted yet
    5. send_activity_warnings  — weekly warning for members with low participation
"""

import logging
import os

import asyncpg

logger = logging.getLogger(__name__)

APP_URL = os.environ.get("APP_URL", "").rstrip("/")
VOTE_PAGE_URL = f"{APP_URL}/vote"


# ---------------------------------------------------------------------------
# Broadcast helper
# ---------------------------------------------------------------------------

async def send_to_users(bot, chat_ids: list[str], message: str) -> None:
    """Send an HTML message to each chat ID, logging individual delivery failures."""
    for chat_id in chat_ids:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            logger.warning("Failed to deliver to chat_id=%s: %s", chat_id, exc)


# ---------------------------------------------------------------------------
# 1. New topic vote notification
# ---------------------------------------------------------------------------

async def notify_new_topic_vote(
    bot,
    pool: asyncpg.Pool,
    topic: str,
    author: str,
    category: str,
    difficulty_label: str,
    threshold: int,
    deadline: str,
) -> None:
    """Notify all active linked members that a new topic has been proposed."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tg_chatid FROM accounts "
            "WHERE acc_type = 'active' AND tg_chatid IS NOT NULL"
        )
    chat_ids = [r["tg_chatid"] for r in rows]
    if not chat_ids:
        return

    message = (
        f"<b>📋 新辯題待表決</b>\n\n"
        f"辯題：{topic}\n"
        f"類別：{category}　｜　{difficulty_label}\n"
        f"提出者：{author}\n"
        f"入庫門檻：{threshold} 票　｜　截止：{deadline} 23:59\n\n"
        f"<a href='{VOTE_PAGE_URL}'>➡️ 立即前往投票</a>"
    )
    await send_to_users(bot, chat_ids, message)
    logger.info("Sent new_topic notification for '%s' to %d members.", topic, len(chat_ids))


# ---------------------------------------------------------------------------
# 2. New deposition vote notification
# ---------------------------------------------------------------------------

async def notify_new_depose_vote(
    bot,
    pool: asyncpg.Pool,
    topic: str,
    mover: str,
    reasons: list[str],
    threshold: int,
    deadline: str,
) -> None:
    """Notify all active linked members that a deposition motion has been filed."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tg_chatid FROM accounts "
            "WHERE acc_type = 'active' AND tg_chatid IS NOT NULL"
        )
    chat_ids = [r["tg_chatid"] for r in rows]
    if not chat_ids:
        return

    reasons_str = "；".join(reasons) if reasons else "（未提供）"
    message = (
        f"<b>⚠️ 新罷免動議</b>\n\n"
        f"辯題：{topic}\n"
        f"提出者：{mover}\n"
        f"罷免原因：{reasons_str}\n"
        f"罷免門檻：{threshold} 票　｜　截止：{deadline} 23:59\n\n"
        f"<a href='{VOTE_PAGE_URL}'>➡️ 立即前往投票</a>"
    )
    await send_to_users(bot, chat_ids, message)
    logger.info("Sent new_depose notification for '%s' to %d members.", topic, len(chat_ids))


# ---------------------------------------------------------------------------
# 3. Vote result notification
# ---------------------------------------------------------------------------

async def notify_vote_result(
    bot,
    pool: asyncpg.Pool,
    topic: str,
    result: str,
    vote_type: str,
    agree_count: int,
    against_count: int,
    threshold: int,
) -> None:
    """Broadcast vote outcome to all active linked members."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tg_chatid FROM accounts "
            "WHERE tg_chatid IS NOT NULL"
        )
    chat_ids = [r["tg_chatid"] for r in rows]
    if not chat_ids:
        return

    if vote_type == "topic":
        if result == "passed":
            headline = "✅ 辯題已通過入庫"
        else:
            headline = "❌ 辯題已被否決"
    else:  # depose
        if result == "passed":
            headline = "🗑️ 罷免動議通過，辯題已從辯題庫移除"
        else:
            headline = "✅ 罷免動議被否決，辯題繼續保留"

    message = (
        f"<b>{headline}</b>\n\n"
        f"辯題：{topic}\n"
        f"最終票數 — 同意：{agree_count}　不同意：{against_count}　門檻：{threshold}\n\n"
        f"<a href='{VOTE_PAGE_URL}'>查看投票記錄</a>"
    )
    await send_to_users(bot, chat_ids, message)
    logger.info(
        "Sent vote_result (%s/%s) for '%s' to %d members.",
        vote_type, result, topic, len(chat_ids)
    )


# ---------------------------------------------------------------------------
# 4. 24-hour deadline reminders (scheduler-driven)
# ---------------------------------------------------------------------------

_TOPIC_24H_SQL = """
SELECT
    tv.topic,
    tv.deadline,
    a.tg_chatid
FROM topic_votes tv
CROSS JOIN accounts a
WHERE tv.status = 'pending'
  AND a.acc_type = 'active'
  AND a.tg_chatid IS NOT NULL
  AND tv.deadline = CURRENT_DATE + INTERVAL '1 day'
  AND NOT EXISTS (
      SELECT 1 FROM topic_vote_ballots b
      WHERE b.topic = tv.topic
        AND b.user_id = a.userid
  )
"""

_DEPOSE_24H_SQL = """
SELECT
    tdv.topic,
    tdv.deadline,
    a.tg_chatid
FROM topic_depose_votes tdv
CROSS JOIN accounts a
WHERE tdv.status = 'pending'
  AND a.acc_type = 'active'
  AND a.tg_chatid IS NOT NULL
  AND tdv.deadline = CURRENT_DATE + INTERVAL '1 day'
  AND NOT EXISTS (
      SELECT 1 FROM depose_vote_ballots b
      WHERE b.topic = tdv.topic
        AND b.user_id = a.userid
  )
"""


async def send_deadline_reminders(bot, pool: asyncpg.Pool) -> None:
    """
    Send 24-hour deadline reminders to members who have not yet voted
    on topics/depositions expiring tomorrow.
    Called daily at 09:00 HKT by the scheduler.
    """
    async with pool.acquire() as conn:
        topic_rows = await conn.fetch(_TOPIC_24H_SQL)
        depose_rows = await conn.fetch(_DEPOSE_24H_SQL)

    for row in topic_rows:
        msg = (
            f"<b>⏰ 投票截止提醒</b>\n\n"
            f"辯題「{row['topic']}」將於明日截止，你尚未投票。\n"
            f"截止日期：{row['deadline']} 23:59\n\n"
            f"<a href='{VOTE_PAGE_URL}'>➡️ 立即前往投票</a>"
        )
        await send_to_users(bot, [row["tg_chatid"]], msg)

    for row in depose_rows:
        msg = (
            f"<b>⏰ 罷免投票截止提醒</b>\n\n"
            f"罷免動議「{row['topic']}」將於明日截止，你尚未投票。\n"
            f"截止日期：{row['deadline']} 23:59\n\n"
            f"<a href='{VOTE_PAGE_URL}'>➡️ 立即前往投票</a>"
        )
        await send_to_users(bot, [row["tg_chatid"]], msg)

    total = len(topic_rows) + len(depose_rows)
    logger.info("Sent %d deadline reminders.", total)


# ---------------------------------------------------------------------------
# 5. Activity warnings (scheduler-driven, weekly)
# ---------------------------------------------------------------------------

_ACTIVITY_WARNING_SQL = """
WITH all_votes AS (
    SELECT tv.topic, 'tv' AS src, tv.created_at FROM topic_votes tv
    UNION ALL
    SELECT tdv.topic, 'tdv' AS src, tdv.created_at FROM topic_depose_votes tdv
),
vote_events AS (
    SELECT DISTINCT topic, src FROM all_votes
),
total_events AS (
    SELECT COUNT(*) AS total FROM vote_events
),
last10 AS (
    SELECT topic, src
    FROM (
        SELECT topic, src, created_at,
               ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rn
        FROM all_votes
    ) ranked
    WHERE rn <= 10
),
member_stats AS (
    SELECT
        a.userid,
        a.tg_chatid,
        COUNT(DISTINCT CASE WHEN b.user_id = a.userid THEN ve.topic || ve.src END) AS participated,
        COUNT(DISTINCT CASE WHEN b.user_id = a.userid
                            THEN l10.topic || l10.src END) AS last10_count,
        te.total
    FROM accounts a
    CROSS JOIN total_events te
    LEFT JOIN vote_events ve ON TRUE
    LEFT JOIN (
        SELECT tvb.topic, 'tv' AS src, tvb.user_id FROM topic_vote_ballots tvb
        UNION ALL
        SELECT dvb.topic, 'tdv' AS src, dvb.user_id FROM depose_vote_ballots dvb
    ) b ON b.topic = ve.topic AND b.src = ve.src AND b.user_id = a.userid
    LEFT JOIN last10 l10 ON TRUE
    WHERE a.acc_type IN ('active', 'inactive')
      AND a.tg_chatid IS NOT NULL
    GROUP BY a.userid, a.tg_chatid, te.total
)
SELECT
    userid,
    tg_chatid,
    participated,
    total,
    CASE WHEN total > 0
         THEN ROUND(participated::numeric / total * 100, 1)
         ELSE 0 END AS rate_pct,
    last10_count
FROM member_stats
WHERE total > 0
  AND (
      (total > 0 AND participated::numeric / total < 0.4)
      OR last10_count < 3
  )
"""


async def send_activity_warnings(bot, pool: asyncpg.Pool) -> None:
    """
    Warn members at risk of losing active status due to low participation.
    Called weekly on Mondays at 08:00 HKT by the scheduler.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(_ACTIVITY_WARNING_SQL)

    for row in rows:
        rate = float(row["rate_pct"] or 0)
        last10 = int(row["last10_count"] or 0)
        warnings = []
        if row["total"] > 0 and row["participated"] / row["total"] < 0.4:
            warnings.append(f"整體投票率：{rate:.1f}%（需達 40%）")
        if last10 < 3:
            warnings.append(f"最近 10 次投票參與：{last10} 次（需達 3 次）")
        if not warnings:
            continue

        msg = (
            f"<b>📉 活躍度提醒</b>\n\n"
            f"你好，{row['userid']}！你的委員帳戶參與率未達標準：\n"
            + "\n".join(f"• {w}" for w in warnings)
            + "\n\n如未改善，帳戶將轉為非活躍狀態，屆時將不能提出新辯題或罷免動議。\n\n"
            f"<a href='{VOTE_PAGE_URL}'>➡️ 立即前往投票</a>"
        )
        await send_to_users(bot, [row["tg_chatid"]], msg)

    logger.info("Sent %d activity warnings.", len(rows))
