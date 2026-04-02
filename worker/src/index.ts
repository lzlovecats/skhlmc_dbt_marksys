import { Client } from "pg";
import { TABLES } from "./dbNames";

type HyperdriveBinding = {
  connectionString: string;
};

export type Env = {
  APP_URL: string;
  BOT_TOKEN: string;
  TELEGRAM_WEBHOOK_SECRET: string;
  HYPERDRIVE: HyperdriveBinding;
};

type TelegramUpdate = {
  message?: TelegramMessage;
};

type TelegramMessage = {
  chat: { id: number | string };
  from?: { id: number | string };
  text?: string;
};

type PendingRow = {
  topic_text: string;
  deadline_date: string | Date;
  approval_threshold: number | string;
  agree_count: number | string | null;
  against_count: number | string | null;
};

type QueueRow = {
  id: number;
  notification_type: string;
  payload: unknown;
  processing_token: string;
};

type ActivityWarningRow = {
  user_id: string;
  telegram_chat_id: string;
  participated: number | string;
  total_votes: number | string;
  rate_pct: number | string | null;
  last10_count: number | string | null;
};

const DRAIN_QUEUE_CRON = "*/15 * * * *";
const DEADLINE_REMINDERS_CRON = "0 1 * * *";
const ACTIVITY_WARNINGS_CRON = "0 9 * * FRI";
const CLAIM_STALE_AFTER = "1 hour";
const BOT_API_BASE = "https://api.telegram.org";

const STATUS_LABELS: Record<string, string> = {
  admin: "管理員",
  active: "活躍成員",
  inactive: "非活躍成員",
};

const TOPIC_24H_SQL = `
SELECT
    tv.topic_text,
    tv.deadline_date,
    a.telegram_chat_id
FROM ${TABLES.topicVotes} tv
CROSS JOIN ${TABLES.accounts} a
WHERE tv.status = 'pending'
  AND a.account_status = 'active'
  AND a.telegram_chat_id IS NOT NULL
  AND tv.deadline_date = CURRENT_DATE + INTERVAL '1 day'
  AND NOT EXISTS (
      SELECT 1 FROM ${TABLES.topicVoteBallots} b
      WHERE b.topic_text = tv.topic_text
        AND b.user_id = a.user_id
  )
`;

const DEPOSE_24H_SQL = `
SELECT
    tdv.topic_text,
    tdv.deadline_date,
    a.telegram_chat_id
FROM ${TABLES.topicRemovalVotes} tdv
CROSS JOIN ${TABLES.accounts} a
WHERE tdv.status = 'pending'
  AND a.account_status = 'active'
  AND a.telegram_chat_id IS NOT NULL
  AND tdv.deadline_date = CURRENT_DATE + INTERVAL '1 day'
  AND NOT EXISTS (
      SELECT 1 FROM ${TABLES.topicRemovalVoteBallots} b
      WHERE b.topic_text = tdv.topic_text
        AND b.user_id = a.user_id
  )
`;

const ACTIVITY_WARNING_SQL = `
WITH all_votes AS (
    SELECT tv.topic_text, 'tv' AS vote_source, tv.created_at FROM ${TABLES.topicVotes} tv
    UNION ALL
    SELECT tdv.topic_text, 'tdv' AS vote_source, tdv.created_at FROM ${TABLES.topicRemovalVotes} tdv
),
vote_events AS (
    SELECT DISTINCT topic_text, vote_source FROM all_votes
),
total_events AS (
    SELECT COUNT(*) AS total_votes FROM vote_events
),
last10 AS (
    SELECT topic_text, vote_source
    FROM (
        SELECT topic_text, vote_source, created_at,
               ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rn
        FROM all_votes
    ) ranked
    WHERE rn <= 10
),
member_stats AS (
    SELECT
        a.user_id,
        a.telegram_chat_id,
        COUNT(DISTINCT CASE WHEN b.user_id = a.user_id THEN ve.topic_text || ve.vote_source END) AS participated,
        COUNT(DISTINCT CASE WHEN b.user_id = a.user_id
                            THEN l10.topic_text || l10.vote_source END) AS last10_count,
        te.total_votes
    FROM ${TABLES.accounts} a
    CROSS JOIN total_events te
    LEFT JOIN vote_events ve ON TRUE
    LEFT JOIN (
        SELECT tvb.topic_text, 'tv' AS vote_source, tvb.user_id FROM ${TABLES.topicVoteBallots} tvb
        UNION ALL
        SELECT dvb.topic_text, 'tdv' AS vote_source, dvb.user_id FROM ${TABLES.topicRemovalVoteBallots} dvb
    ) b ON b.topic_text = ve.topic_text AND b.vote_source = ve.vote_source AND b.user_id = a.user_id
    LEFT JOIN last10 l10 ON TRUE
    WHERE a.account_status IN ('active', 'inactive')
      AND a.telegram_chat_id IS NOT NULL
    GROUP BY a.user_id, a.telegram_chat_id, te.total_votes
)
SELECT
    user_id,
    telegram_chat_id,
    participated,
    total_votes,
    CASE WHEN total_votes > 0
         THEN ROUND(participated::numeric / total_votes * 100, 1)
         ELSE 0 END AS rate_pct,
    last10_count
FROM member_stats
WHERE total_votes > 0
  AND (
      (total_votes > 0 AND participated::numeric / total_votes < 0.4)
      OR last10_count < 3
  )
`;

export function buildVotePageUrl(appUrl: string): string {
  return `${appUrl.replace(/\/$/, "")}/vote`;
}

export function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function normalizeCommand(text?: string | null): { command: string; args: string[] } | null {
  if (!text) {
    return null;
  }

  const trimmed = text.trim();
  if (!trimmed.startsWith("/")) {
    return null;
  }

  const parts = trimmed.split(/\s+/);
  const command = parts[0].slice(1).split("@")[0].toLowerCase();
  return {
    command: `/${command}`,
    args: parts.slice(1),
  };
}

export function buildPendingMessage(
  topicRows: PendingRow[],
  deposeRows: PendingRow[],
  votePageUrl: string,
): string {
  const lines = ["<b>📋 待表決議案</b>\n"];

  if (topicRows.length > 0) {
    lines.push("<b>— 辯題入庫投票 —</b>");
    for (const row of topicRows) {
      lines.push(
        `• ${escapeHtml(row.topic_text)}\n` +
          `  同意 ${toNumber(row.agree_count)} ／ 不同意 ${toNumber(row.against_count)}（門檻 ${toNumber(row.approval_threshold)}）` +
          `  截止：${formatDate(row.deadline_date)}`,
      );
    }
  } else {
    lines.push("目前沒有待表決的辯題入庫投票。");
  }

  lines.push("");

  if (deposeRows.length > 0) {
    lines.push("<b>— 罷免動議投票 —</b>");
    for (const row of deposeRows) {
      lines.push(
        `• ${escapeHtml(row.topic_text)}\n` +
          `  同意 ${toNumber(row.agree_count)} ／ 不同意 ${toNumber(row.against_count)}（門檻 ${toNumber(row.approval_threshold)}）` +
          `  截止：${formatDate(row.deadline_date)}`,
      );
    }
  } else {
    lines.push("目前沒有待表決的罷免動議。");
  }

  lines.push(`\n<a href='${escapeHtml(votePageUrl)}'>➡️ 前往投票</a>`);
  return lines.join("\n");
}

export function buildVoteResultMessage(
  votePageUrl: string,
  payload: {
    topic: string;
    result: string;
    vote_type: string;
    agree_count: number | string;
    against_count: number | string;
    threshold: number | string;
  },
): string {
  let headline = "";
  if (payload.vote_type === "topic") {
    headline = payload.result === "passed" ? "✅ 辯題已通過入庫" : "❌ 辯題已被否決";
  } else {
    headline =
      payload.result === "passed"
        ? "🗑️ 罷免動議通過，辯題已從辯題庫移除"
        : "✅ 罷免動議被否決，辯題繼續保留";
  }

  return (
    `<b>${headline}</b>\n\n` +
    `辯題：${escapeHtml(payload.topic)}\n` +
    `最終票數 — 同意：${toNumber(payload.agree_count)}　不同意：${toNumber(payload.against_count)}　門檻：${toNumber(payload.threshold)}\n\n` +
    `<a href='${escapeHtml(votePageUrl)}'>查看投票記錄</a>`
  );
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    return handleRequest(request, env);
  },

  async scheduled(controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(handleScheduled(controller.cron, env));
  },
} satisfies ExportedHandler<Env>;

async function handleRequest(request: Request, env: Env): Promise<Response> {
  const url = new URL(request.url);

  if (request.method === "GET" && url.pathname === "/health") {
    return Response.json({ ok: true });
  }

  if (url.pathname !== `/telegram/${env.TELEGRAM_WEBHOOK_SECRET}`) {
    return new Response("Not Found", { status: 404 });
  }

  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  const update = (await request.json()) as TelegramUpdate;
  const message = update.message;
  if (!message?.text || !message.from) {
    return new Response("ok");
  }

  try {
    await withClient(env, async (client) => {
      await handleTelegramCommand(client, env, message);
    });
  } catch (error) {
    console.error("Webhook command handling failed", error);
    await sendMessage(env, String(message.chat.id), "系統暫時未能處理你的指令，請稍後再試。");
  }

  return new Response("ok");
}

async function handleScheduled(cron: string, env: Env): Promise<void> {
  await withClient(env, async (client) => {
    if (cron === DRAIN_QUEUE_CRON) {
      await drainQueue(client, env);
      return;
    }

    if (cron === DEADLINE_REMINDERS_CRON) {
      await sendDeadlineReminders(client, env);
      return;
    }

    if (cron === ACTIVITY_WARNINGS_CRON) {
      await sendActivityWarnings(client, env);
      return;
    }

    console.warn("Unhandled cron expression", cron);
  });
}

async function withClient<T>(env: Env, run: (client: Client) => Promise<T>): Promise<T> {
  const client = new Client({
    connectionString: env.HYPERDRIVE.connectionString,
  });

  await client.connect();
  try {
    return await run(client);
  } finally {
    await client.end();
  }
}

async function handleTelegramCommand(client: Client, env: Env, message: TelegramMessage): Promise<void> {
  const parsed = normalizeCommand(message.text);
  if (!parsed) {
    return;
  }

  switch (parsed.command) {
    case "/link":
      await cmdLink(client, env, message, parsed.args);
      return;
    case "/unlink":
      await cmdUnlink(client, env, message);
      return;
    case "/status":
      await cmdStatus(client, env, message);
      return;
    case "/pending":
      await cmdPending(client, env, message);
      return;
    case "/myvotes":
      await cmdMyVotes(client, env, message);
      return;
    case "/help":
      await cmdHelp(env, message);
      return;
    default:
      await cmdHelp(env, message);
  }
}

async function cmdLink(client: Client, env: Env, message: TelegramMessage, args: string[]): Promise<void> {
  if (args.length === 0) {
    await sendMessage(env, String(message.chat.id), "用法：/link <你的個人帳戶用戶名稱>\n例如：/link leungph");
    return;
  }

  const userId = args[0].trim();
  const tgChatId = String(message.chat.id);
  const tgUserId = String(message.from?.id ?? "");

  const account = await client.query<{ user_id: string; account_status: string }>(
    `SELECT user_id, account_status FROM ${TABLES.accounts} WHERE user_id = $1`,
    [userId],
  );
  if ((account.rowCount ?? 0) === 0) {
    await sendMessage(env, tgChatId, `找不到委員帳戶「${userId}」。請確認用戶名稱是否正確。`);
    return;
  }

  const existing = await client.query<{ user_id: string }>(
    `SELECT user_id FROM ${TABLES.accounts} WHERE telegram_chat_id = $1 AND user_id != $2`,
    [tgChatId, userId],
  );
  if ((existing.rowCount ?? 0) > 0) {
    await sendMessage(
      env,
      tgChatId,
      `此 Telegram 帳戶已連結至委員帳戶「${existing.rows[0].user_id}」。\n請先發送 /unlink 解除連結後再試。`,
    );
    return;
  }

  await client.query(
    `UPDATE ${TABLES.accounts} SET telegram_user_id = $1, telegram_chat_id = $2 WHERE user_id = $3`,
    [tgUserId, tgChatId, userId],
  );

  const accType = account.rows[0].account_status;
  const statusLabel = STATUS_LABELS[accType] ?? accType;
  await sendMessage(
    env,
    tgChatId,
    `✅ 連結成功！\n\n委員帳戶：${userId}\n帳戶狀態：${statusLabel}\n\n你將會收到辯題投票通知。\n前往投票：${buildVotePageUrl(env.APP_URL)}`,
  );
}

async function cmdUnlink(client: Client, env: Env, message: TelegramMessage): Promise<void> {
  const tgChatId = String(message.chat.id);
  const result = await client.query(
    `UPDATE ${TABLES.accounts} SET telegram_user_id = NULL, telegram_chat_id = NULL WHERE telegram_chat_id = $1`,
    [tgChatId],
  );
  if ((result.rowCount ?? 0) === 1) {
    await sendMessage(env, tgChatId, "已成功解除 Telegram 帳戶連結。");
    return;
  }

  await sendMessage(env, tgChatId, "此 Telegram 帳戶未連結任何委員帳戶。\n使用 /link <userid> 進行連結。");
}

async function cmdStatus(client: Client, env: Env, message: TelegramMessage): Promise<void> {
  const tgChatId = String(message.chat.id);
  const result = await client.query<{ user_id: string; account_status: string }>(
    `SELECT user_id, account_status FROM ${TABLES.accounts} WHERE telegram_chat_id = $1`,
    [tgChatId],
  );
  if ((result.rowCount ?? 0) === 0) {
    await sendMessage(env, tgChatId, "此 Telegram 帳戶未連結任何委員帳戶。\n使用 /link <userid> 進行連結。");
    return;
  }

  const row = result.rows[0];
  const statusLabel = STATUS_LABELS[row.account_status] ?? row.account_status;
  await sendMessage(env, tgChatId, `已連結帳戶：${row.user_id}\n帳戶狀態：${statusLabel}`);
}

async function cmdPending(client: Client, env: Env, message: TelegramMessage): Promise<void> {
  const topicRows = await client.query<PendingRow>(`
    SELECT tv.topic_text, tv.deadline_date, tv.approval_threshold,
           COUNT(CASE WHEN b.vote_choice = 'agree' THEN 1 END)   AS agree_count,
           COUNT(CASE WHEN b.vote_choice = 'against' THEN 1 END) AS against_count
    FROM ${TABLES.topicVotes} tv
    LEFT JOIN ${TABLES.topicVoteBallots} b ON b.topic_text = tv.topic_text
    WHERE tv.status = 'pending'
    GROUP BY tv.topic_text, tv.deadline_date, tv.approval_threshold
    ORDER BY tv.deadline_date ASC
  `);
  const deposeRows = await client.query<PendingRow>(`
    SELECT tdv.topic_text, tdv.deadline_date, tdv.approval_threshold,
           COUNT(CASE WHEN b.vote_choice = 'agree' THEN 1 END)   AS agree_count,
           COUNT(CASE WHEN b.vote_choice = 'against' THEN 1 END) AS against_count
    FROM ${TABLES.topicRemovalVotes} tdv
    LEFT JOIN ${TABLES.topicRemovalVoteBallots} b ON b.topic_text = tdv.topic_text
    WHERE tdv.status = 'pending'
    GROUP BY tdv.topic_text, tdv.deadline_date, tdv.approval_threshold
    ORDER BY tdv.deadline_date ASC
  `);

  await sendHtml(
    env,
    String(message.chat.id),
    buildPendingMessage(topicRows.rows, deposeRows.rows, buildVotePageUrl(env.APP_URL)),
    true,
  );
}

async function cmdMyVotes(client: Client, env: Env, message: TelegramMessage): Promise<void> {
  const tgChatId = String(message.chat.id);
  const account = await client.query<{ user_id: string; account_status: string }>(
    `SELECT user_id, account_status FROM ${TABLES.accounts} WHERE telegram_chat_id = $1`,
    [tgChatId],
  );
  if ((account.rowCount ?? 0) === 0) {
    await sendMessage(env, tgChatId, "此 Telegram 帳戶未連結任何委員帳戶。\n使用 /link <userid> 進行連結。");
    return;
  }

  const userId = account.rows[0].user_id;
  const stats = await client.query<{
    total_votes: number | string | null;
    participated_votes: number | string | null;
    last10_count: number | string | null;
  }>(
    `
    WITH vote_events AS (
        SELECT topic_text, 'tv' AS vote_source FROM ${TABLES.topicVotes}
        UNION ALL
        SELECT topic_text, 'tdv' AS vote_source FROM ${TABLES.topicRemovalVotes}
    ),
    ballots AS (
        SELECT topic_text, 'tv' AS vote_source FROM ${TABLES.topicVoteBallots} WHERE user_id = $1
        UNION ALL
        SELECT topic_text, 'tdv' AS vote_source FROM ${TABLES.topicRemovalVoteBallots} WHERE user_id = $1
    ),
    last10 AS (
        SELECT topic_text, vote_source
        FROM (
            SELECT topic_text, vote_source, created_at,
                   ROW_NUMBER() OVER (ORDER BY created_at DESC) AS rn
            FROM (
                SELECT topic_text, 'tv' AS vote_source, created_at FROM ${TABLES.topicVotes}
                UNION ALL
                SELECT topic_text, 'tdv' AS vote_source, created_at FROM ${TABLES.topicRemovalVotes}
            ) all_ev
        ) ranked
        WHERE rn <= 10
    )
    SELECT
        (SELECT COUNT(*) FROM vote_events) AS total_votes,
        (SELECT COUNT(*) FROM ballots) AS participated_votes,
        (SELECT COUNT(*) FROM ballots b JOIN last10 l ON b.topic_text = l.topic_text AND b.vote_source = l.vote_source) AS last10_count
    `,
    [userId],
  );

  const statRow = stats.rows[0];
  const total = toNumber(statRow.total_votes);
  const participated = toNumber(statRow.participated_votes);
  const last10 = toNumber(statRow.last10_count);
  const rate = total > 0 ? roundOneDecimal((participated / total) * 100) : 0;
  const accType = account.rows[0].account_status;
  const statusLabel = STATUS_LABELS[accType] ?? accType;
  const rateOk = rate >= 40 ? "✅" : "⚠️";
  const last10Ok = last10 >= 3 ? "✅" : "⚠️";

  await sendHtml(
    env,
    tgChatId,
    `<b>📊 個人投票紀錄 — ${escapeHtml(userId)}</b>\n\n` +
      `帳戶狀態：${escapeHtml(statusLabel)}\n` +
      `${rateOk} 整體投票率：${rate}%（${participated} / ${total} 次）\n` +
      `${last10Ok} 最近 10 次參與：${last10} 次\n\n` +
      "標準：整體投票率 ≥ 40% 且最近 10 次 ≥ 3 次",
  );
}

async function cmdHelp(env: Env, message: TelegramMessage): Promise<void> {
  await sendHtml(
    env,
    String(message.chat.id),
    "<b>聖呂中辯電子分紙系統 — Telegram Bot 使用說明</b>\n\n" +
      "/link &lt;userid&gt; — 連結你的委員帳戶\n" +
      "/unlink — 解除 Telegram 連結\n" +
      "/status — 查看連結狀態及帳戶類型\n" +
      "/pending — 查看所有待表決的辯題及罷免動議\n" +
      "/myvotes — 查看個人投票參與率\n" +
      "/help — 顯示此說明\n\n" +
      `前往投票系統：${escapeHtml(buildVotePageUrl(env.APP_URL))}`,
    true,
  );
}

async function drainQueue(client: Client, env: Env): Promise<void> {
  const processingToken = crypto.randomUUID();
  const rows = await claimQueueRows(client, processingToken);
  if (rows.length === 0) {
    return;
  }

  for (const row of rows) {
    const payload = typeof row.payload === "string" ? JSON.parse(row.payload) : row.payload;
    try {
      switch (row.notification_type) {
        case "new_topic":
          await notifyNewTopicVote(client, env, payload as NewTopicPayload);
          break;
        case "new_depose":
          await notifyNewDeposeVote(client, env, payload as NewDeposePayload);
          break;
        case "vote_result":
          await notifyVoteResult(client, env, payload as VoteResultPayload);
          break;
        default:
          throw new Error(`Unknown notification_type '${row.notification_type}'`);
      }

      await markQueueRowProcessed(client, row.id, row.processing_token);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      console.error("Failed to process queue row", { id: row.id, notificationType: row.notification_type, error: message });
      await releaseQueueRow(client, row.id, row.processing_token, message);
    }
  }
}

async function claimQueueRows(client: Client, processingToken: string): Promise<QueueRow[]> {
  await client.query("BEGIN");
  try {
    const result = await client.query<QueueRow>(
      `
      WITH candidates AS (
          SELECT id
          FROM ${TABLES.telegramNotificationQueue}
          WHERE is_processed = FALSE
            AND (
                processing_token IS NULL
                OR processing_started_at < NOW() - INTERVAL '${CLAIM_STALE_AFTER}'
            )
          ORDER BY created_at ASC
          LIMIT 50
          FOR UPDATE SKIP LOCKED
      )
      UPDATE ${TABLES.telegramNotificationQueue} AS queue
      SET processing_token = $1,
          processing_started_at = NOW(),
          last_error_message = NULL
      FROM candidates
      WHERE queue.id = candidates.id
      RETURNING queue.id, queue.notification_type, queue.payload, queue.processing_token
      `,
      [processingToken],
    );
    await client.query("COMMIT");
    return result.rows;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  }
}

async function markQueueRowProcessed(client: Client, id: number, processingToken: string): Promise<void> {
  await client.query(
    `
    UPDATE ${TABLES.telegramNotificationQueue}
    SET is_processed = TRUE,
        processing_token = NULL,
        processing_started_at = NULL,
        last_error_message = NULL
    WHERE id = $1
      AND processing_token = $2
    `,
    [id, processingToken],
  );
}

async function releaseQueueRow(client: Client, id: number, processingToken: string, errorMessage: string): Promise<void> {
  await client.query(
    `
    UPDATE ${TABLES.telegramNotificationQueue}
    SET processing_token = NULL,
        processing_started_at = NULL,
        last_error_message = $3
    WHERE id = $1
      AND processing_token = $2
    `,
    [id, processingToken, errorMessage.slice(0, 500)],
  );
}

type NewTopicPayload = {
  topic: string;
  author: string;
  category: string;
  difficulty_label: string;
  threshold: number | string;
  deadline: string;
};

type NewDeposePayload = {
  topic: string;
  mover: string;
  reasons: string[];
  threshold: number | string;
  deadline: string;
};

type VoteResultPayload = {
  topic: string;
  result: string;
  vote_type: string;
  agree_count: number | string;
  against_count: number | string;
  threshold: number | string;
};

async function notifyNewTopicVote(client: Client, env: Env, payload: NewTopicPayload): Promise<void> {
  const rows = await client.query<{ telegram_chat_id: string }>(
    `SELECT telegram_chat_id FROM ${TABLES.accounts} WHERE account_status = 'active' AND telegram_chat_id IS NOT NULL`,
  );
  const chatIds = rows.rows.map((row) => row.telegram_chat_id);
  if (chatIds.length === 0) {
    return;
  }

  const message =
    "<b>📋 新辯題待表決</b>\n\n" +
    `辯題：${escapeHtml(payload.topic)}\n` +
    `類別：${escapeHtml(payload.category)}　｜　${escapeHtml(payload.difficulty_label)}\n` +
    `提出者：${escapeHtml(payload.author)}\n` +
    `入庫門檻：${toNumber(payload.threshold)} 票　｜　截止：${escapeHtml(payload.deadline)} 23:59\n\n` +
    `<a href='${escapeHtml(buildVotePageUrl(env.APP_URL))}'>➡️ 立即前往投票</a>`;

  await sendToUsersStrict(env, chatIds, message);
}

async function notifyNewDeposeVote(client: Client, env: Env, payload: NewDeposePayload): Promise<void> {
  const rows = await client.query<{ telegram_chat_id: string }>(
    `SELECT telegram_chat_id FROM ${TABLES.accounts} WHERE account_status = 'active' AND telegram_chat_id IS NOT NULL`,
  );
  const chatIds = rows.rows.map((row) => row.telegram_chat_id);
  if (chatIds.length === 0) {
    return;
  }

  const reasons = payload.reasons.length > 0 ? payload.reasons.map(escapeHtml).join("；") : "（未提供）";
  const message =
    "<b>⚠️ 新罷免動議</b>\n\n" +
    `辯題：${escapeHtml(payload.topic)}\n` +
    `提出者：${escapeHtml(payload.mover)}\n` +
    `罷免原因：${reasons}\n` +
    `罷免門檻：${toNumber(payload.threshold)} 票　｜　截止：${escapeHtml(payload.deadline)} 23:59\n\n` +
    `<a href='${escapeHtml(buildVotePageUrl(env.APP_URL))}'>➡️ 立即前往投票</a>`;

  await sendToUsersStrict(env, chatIds, message);
}

async function notifyVoteResult(client: Client, env: Env, payload: VoteResultPayload): Promise<void> {
  const rows = await client.query<{ telegram_chat_id: string }>(
    `SELECT telegram_chat_id FROM ${TABLES.accounts} WHERE telegram_chat_id IS NOT NULL`,
  );
  const chatIds = rows.rows.map((row) => row.telegram_chat_id);
  if (chatIds.length === 0) {
    return;
  }

  await sendToUsersStrict(env, chatIds, buildVoteResultMessage(buildVotePageUrl(env.APP_URL), payload));
}

async function sendDeadlineReminders(client: Client, env: Env): Promise<void> {
  const topicRows = await client.query<{ topic_text: string; deadline_date: string | Date; telegram_chat_id: string }>(TOPIC_24H_SQL);
  const deposeRows = await client.query<{ topic_text: string; deadline_date: string | Date; telegram_chat_id: string }>(DEPOSE_24H_SQL);

  for (const row of topicRows.rows) {
    const message =
      "<b>⏰ 投票截止提醒</b>\n\n" +
      `辯題「${escapeHtml(row.topic_text)}」將於明日截止，你尚未投票。\n` +
      `截止日期：${formatDate(row.deadline_date)} 23:59\n\n` +
      `<a href='${escapeHtml(buildVotePageUrl(env.APP_URL))}'>➡️ 立即前往投票</a>`;
    await sendToUsersBestEffort(env, [row.telegram_chat_id], message);
  }

  for (const row of deposeRows.rows) {
    const message =
      "<b>⏰ 罷免投票截止提醒</b>\n\n" +
      `罷免動議「${escapeHtml(row.topic_text)}」將於明日截止，你尚未投票。\n` +
      `截止日期：${formatDate(row.deadline_date)} 23:59\n\n` +
      `<a href='${escapeHtml(buildVotePageUrl(env.APP_URL))}'>➡️ 立即前往投票</a>`;
    await sendToUsersBestEffort(env, [row.telegram_chat_id], message);
  }
}

async function sendActivityWarnings(client: Client, env: Env): Promise<void> {
  const rows = await client.query<ActivityWarningRow>(ACTIVITY_WARNING_SQL);

  for (const row of rows.rows) {
    const total = toNumber(row.total_votes);
    const participated = toNumber(row.participated);
    const last10 = toNumber(row.last10_count);
    const rate = toNumber(row.rate_pct);
    const warnings: string[] = [];

    if (total > 0 && participated / total < 0.4) {
      warnings.push(`整體投票率：${rate.toFixed(1)}%（需達 40%）`);
    }
    if (last10 < 3) {
      warnings.push(`最近 10 次投票參與：${last10} 次（需達 3 次）`);
    }
    if (warnings.length === 0) {
      continue;
    }

    const message =
      "<b>📉 活躍度提醒</b>\n\n" +
      `你好，${escapeHtml(row.user_id)}！你的委員帳戶參與率未達標準：\n` +
      warnings.map((item) => `• ${escapeHtml(item)}`).join("\n") +
      "\n\n如未改善，帳戶將轉為非活躍狀態，屆時將不能提出新辯題或罷免動議。\n\n" +
      `<a href='${escapeHtml(buildVotePageUrl(env.APP_URL))}'>➡️ 立即前往投票</a>`;
    await sendToUsersBestEffort(env, [row.telegram_chat_id], message);
  }
}

async function sendToUsersStrict(env: Env, chatIds: string[], message: string): Promise<void> {
  const failures: string[] = [];
  for (const chatId of chatIds) {
    try {
      await sendHtml(env, chatId, message, true);
    } catch (error) {
      failures.push(error instanceof Error ? `chat_id=${chatId}: ${error.message}` : `chat_id=${chatId}: ${String(error)}`);
    }
  }

  if (failures.length > 0) {
    throw new Error(failures.join(" | "));
  }
}

async function sendToUsersBestEffort(env: Env, chatIds: string[], message: string): Promise<void> {
  for (const chatId of chatIds) {
    try {
      await sendHtml(env, chatId, message, true);
    } catch (error) {
      console.error("Failed to deliver Telegram message", { chatId, error });
    }
  }
}

async function sendMessage(env: Env, chatId: string, text: string): Promise<void> {
  await callTelegram(env, "sendMessage", {
    chat_id: chatId,
    text,
  });
}

async function sendHtml(env: Env, chatId: string, text: string, disablePreview = false): Promise<void> {
  await callTelegram(env, "sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: disablePreview,
  });
}

async function callTelegram(env: Env, method: string, payload: Record<string, unknown>): Promise<unknown> {
  const response = await fetch(`${BOT_API_BASE}/bot${env.BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  const body = (await response.json()) as { ok: boolean; description?: string; result?: unknown };
  if (!response.ok || !body.ok) {
    throw new Error(body.description ?? `Telegram API ${method} failed with status ${response.status}`);
  }

  return body.result;
}

function toNumber(value: number | string | null | undefined): number {
  if (value === null || value === undefined || value === "") {
    return 0;
  }

  const num = Number(value);
  return Number.isFinite(num) ? num : 0;
}

function roundOneDecimal(value: number): number {
  return Math.round(value * 10) / 10;
}

function formatDate(value: string | Date): string {
  if (value instanceof Date) {
    const year = value.getUTCFullYear();
    const month = String(value.getUTCMonth() + 1).padStart(2, "0");
    const day = String(value.getUTCDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }
  return String(value).slice(0, 10);
}
