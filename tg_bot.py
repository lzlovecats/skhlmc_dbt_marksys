"""
tg_bot.py — Telegram bot for the SKHLMC Debate Marking System.

Runs as a standalone worker process separate from the Streamlit app.
Shares the same PostgreSQL database via asyncpg.

Required environment variables:
    BOT_TOKEN    — Telegram Bot API token from @BotFather
    DATABASE_URL — PostgreSQL connection string (postgresql://user:pass@host:5432/db)
    APP_URL      — Base URL of the Streamlit app (e.g. https://yourapp.streamlit.app)

Usage:
    python tg_bot.py

Commands exposed to users:
    /link <userid>  — link Telegram account to committee account
    /unlink         — unlink Telegram account
    /status         — show current linkage and account status
    /pending        — list all open votes with current counts
    /myvotes        — show personal participation stats
    /help           — usage guide
"""

import logging
import os

import asyncpg
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from tg_scheduler import setup_scheduler

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
APP_URL = os.environ.get("APP_URL", "").rstrip("/")
VOTE_PAGE_URL = f"{APP_URL}/vote"


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------

async def post_init(application: Application) -> None:
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    application.bot_data["db_pool"] = pool

    scheduler = setup_scheduler(application)
    application.bot_data["scheduler"] = scheduler
    scheduler.start()

    await application.bot.set_my_commands([
        BotCommand("link",    "連結委員帳戶 /link <userid>"),
        BotCommand("unlink",  "解除 Telegram 連結"),
        BotCommand("status",  "查看連結狀態"),
        BotCommand("pending", "查看所有待表決議案"),
        BotCommand("myvotes", "查看個人投票參與率"),
        BotCommand("help",    "使用說明"),
    ])
    logger.info("Bot initialised and scheduler started.")


async def post_shutdown(application: Application) -> None:
    scheduler = application.bot_data.get("scheduler")
    if scheduler and scheduler.running:
        scheduler.shutdown(wait=False)

    pool = application.bot_data.get("db_pool")
    if pool:
        await pool.close()


# ---------------------------------------------------------------------------
# /link <userid>
# ---------------------------------------------------------------------------

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Link the sender's Telegram account to their committee account."""
    if not context.args:
        await update.message.reply_text(
            "用法：/link <你的個人帳戶用戶名稱>\n例如：/link leungph"
        )
        return

    userid = context.args[0].strip()
    tg_chat_id = str(update.effective_chat.id)
    tg_user_id = str(update.effective_user.id)

    pool: asyncpg.Pool = context.bot_data["db_pool"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT userid, acc_type FROM accounts WHERE userid = $1",
            userid,
        )
        if not row:
            await update.message.reply_text(
                f"找不到委員帳戶「{userid}」。請確認用戶名稱是否正確。"
            )
            return

        # Prevent one TG account from linking to two different committee accounts
        existing = await conn.fetchrow(
            "SELECT userid FROM accounts WHERE tg_chatid = $1 AND userid != $2",
            tg_chat_id,
            userid,
        )
        if existing:
            await update.message.reply_text(
                f"此 Telegram 帳戶已連結至委員帳戶「{existing['userid']}」。\n"
                "請先發送 /unlink 解除連結後再試。"
            )
            return

        await conn.execute(
            "UPDATE accounts SET tg_userid = $1, tg_chatid = $2 WHERE userid = $3",
            tg_user_id,
            tg_chat_id,
            userid,
        )

    acc_type = row["acc_type"]
    status_label = {"admin": "管理員", "active": "活躍成員", "inactive": "非活躍成員"}.get(
        acc_type, acc_type
    )
    await update.message.reply_text(
        f"✅ 連結成功！\n\n"
        f"委員帳戶：{userid}\n"
        f"帳戶狀態：{status_label}\n\n"
        f"你將會收到辯題投票通知。\n"
        f"前往投票：{VOTE_PAGE_URL}"
    )
    logger.info("Linked tg_chat_id=%s to userid=%s", tg_chat_id, userid)


# ---------------------------------------------------------------------------
# /unlink
# ---------------------------------------------------------------------------

async def cmd_unlink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unlink the sender's Telegram account from their committee account."""
    tg_chat_id = str(update.effective_chat.id)
    pool: asyncpg.Pool = context.bot_data["db_pool"]
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE accounts SET tg_userid = NULL, tg_chatid = NULL WHERE tg_chatid = $1",
            tg_chat_id,
        )
    if result == "UPDATE 1":
        await update.message.reply_text("已成功解除 Telegram 帳戶連結。")
    else:
        await update.message.reply_text(
            "此 Telegram 帳戶未連結任何委員帳戶。\n使用 /link <userid> 進行連結。"
        )


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the linkage status of the sender's Telegram account."""
    tg_chat_id = str(update.effective_chat.id)
    pool: asyncpg.Pool = context.bot_data["db_pool"]
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT userid, acc_type FROM accounts WHERE tg_chatid = $1",
            tg_chat_id,
        )
    if row:
        status_label = {"admin": "管理員", "active": "活躍成員", "inactive": "非活躍成員"}.get(
            row["acc_type"], row["acc_type"]
        )
        await update.message.reply_text(
            f"已連結帳戶：{row['userid']}\n帳戶狀態：{status_label}"
        )
    else:
        await update.message.reply_text(
            "此 Telegram 帳戶未連結任何委員帳戶。\n使用 /link <userid> 進行連結。"
        )


# ---------------------------------------------------------------------------
# /pending
# ---------------------------------------------------------------------------

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all currently open topic votes and deposition motions."""
    pool: asyncpg.Pool = context.bot_data["db_pool"]
    async with pool.acquire() as conn:
        topic_rows = await conn.fetch(
            """
            SELECT tv.topic, tv.deadline, tv.threshold,
                   COUNT(CASE WHEN b.vote = 'agree' THEN 1 END)   AS agree_count,
                   COUNT(CASE WHEN b.vote = 'against' THEN 1 END) AS against_count
            FROM topic_votes tv
            LEFT JOIN topic_vote_ballots b ON b.topic = tv.topic
            WHERE tv.status = 'pending'
            GROUP BY tv.topic, tv.deadline, tv.threshold
            ORDER BY tv.deadline ASC
            """
        )
        depose_rows = await conn.fetch(
            """
            SELECT tdv.topic, tdv.deadline, tdv.threshold,
                   COUNT(CASE WHEN b.vote = 'agree' THEN 1 END)   AS agree_count,
                   COUNT(CASE WHEN b.vote = 'against' THEN 1 END) AS against_count
            FROM topic_depose_votes tdv
            LEFT JOIN depose_vote_ballots b ON b.topic = tdv.topic
            WHERE tdv.status = 'pending'
            GROUP BY tdv.topic, tdv.deadline, tdv.threshold
            ORDER BY tdv.deadline ASC
            """
        )

    lines = ["<b>📋 待表決議案</b>\n"]

    if topic_rows:
        lines.append("<b>— 辯題入庫投票 —</b>")
        for r in topic_rows:
            lines.append(
                f"• {r['topic']}\n"
                f"  同意 {r['agree_count']} ／ 不同意 {r['against_count']}（門檻 {r['threshold']}）"
                f"  截止：{r['deadline']}"
            )
    else:
        lines.append("目前沒有待表決的辯題入庫投票。")

    lines.append("")

    if depose_rows:
        lines.append("<b>— 罷免動議投票 —</b>")
        for r in depose_rows:
            lines.append(
                f"• {r['topic']}\n"
                f"  同意 {r['agree_count']} ／ 不同意 {r['against_count']}（門檻 {r['threshold']}）"
                f"  截止：{r['deadline']}"
            )
    else:
        lines.append("目前沒有待表決的罷免動議。")

    lines.append(f"\n<a href='{VOTE_PAGE_URL}'>➡️ 前往投票</a>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# /myvotes
# ---------------------------------------------------------------------------

async def cmd_myvotes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the sender's personal voting participation statistics."""
    tg_chat_id = str(update.effective_chat.id)
    pool: asyncpg.Pool = context.bot_data["db_pool"]
    async with pool.acquire() as conn:
        account = await conn.fetchrow(
            "SELECT userid, acc_type FROM accounts WHERE tg_chatid = $1",
            tg_chat_id,
        )
        if not account:
            await update.message.reply_text(
                "此 Telegram 帳戶未連結任何委員帳戶。\n使用 /link <userid> 進行連結。"
            )
            return

        userid = account["userid"]

        stats = await conn.fetchrow(
            """
            WITH vote_events AS (
                SELECT topic, 'tv' AS src FROM topic_votes
                UNION ALL
                SELECT topic, 'tdv' AS src FROM topic_depose_votes
            ),
            ballots AS (
                SELECT topic, 'tv' AS src FROM topic_vote_ballots WHERE user_id = $1
                UNION ALL
                SELECT topic, 'tdv' AS src FROM depose_vote_ballots WHERE user_id = $1
            ),
            last10 AS (
                SELECT topic, src
                FROM (
                    SELECT topic, src, created_at,
                           ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rn
                    FROM (
                        SELECT topic, 'tv' AS src, created_at FROM topic_votes
                        UNION ALL
                        SELECT topic, 'tdv' AS src, created_at FROM topic_depose_votes
                    ) all_ev
                ) ranked
                WHERE rn <= 10
            )
            SELECT
                (SELECT COUNT(*) FROM vote_events) AS total_votes,
                (SELECT COUNT(*) FROM ballots) AS participated,
                (SELECT COUNT(*) FROM ballots b JOIN last10 l ON b.topic = l.topic AND b.src = l.src) AS last10_count
            """,
            userid,
        )

    total = int(stats["total_votes"] or 0)
    participated = int(stats["participated"] or 0)
    last10 = int(stats["last10_count"] or 0)
    rate = round(participated / total * 100, 1) if total > 0 else 0.0

    acc_type = account["acc_type"]
    status_label = {"admin": "管理員", "active": "活躍成員", "inactive": "非活躍成員"}.get(
        acc_type, acc_type
    )

    rate_ok = "✅" if rate >= 40 else "⚠️"
    last10_ok = "✅" if last10 >= 3 else "⚠️"

    await update.message.reply_text(
        f"<b>📊 個人投票紀錄 — {userid}</b>\n\n"
        f"帳戶狀態：{status_label}\n"
        f"{rate_ok} 整體投票率：{rate}%（{participated} / {total} 次）\n"
        f"{last10_ok} 最近 10 次參與：{last10} 次\n\n"
        f"標準：整體投票率 ≥ 40% 且最近 10 次 ≥ 3 次",
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>聖呂中辯電子分紙系統 — Telegram Bot 使用說明</b>\n\n"
        "/link &lt;userid&gt; — 連結你的委員帳戶\n"
        "/unlink — 解除 Telegram 連結\n"
        "/status — 查看連結狀態及帳戶類型\n"
        "/pending — 查看所有待表決的辯題及罷免動議\n"
        "/myvotes — 查看個人投票參與率\n"
        "/help — 顯示此說明\n\n"
        f"前往投票系統：{VOTE_PAGE_URL}",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("link",    cmd_link))
    app.add_handler(CommandHandler("unlink",  cmd_unlink))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("myvotes", cmd_myvotes))
    app.add_handler(CommandHandler("help",    cmd_help))

    logger.info("Starting bot in polling mode...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
