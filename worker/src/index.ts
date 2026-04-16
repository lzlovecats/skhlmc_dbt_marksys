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
  chat: { id: number | string; type?: string };
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

type ActivityRow = {
  user_id: string;
  telegram_chat_id: string;
  account_status: string;
  participated_votes: number | string;
  total_votes: number | string;
  overall_rate_pct: number | string | null;
  last10_participated: number | string | null;
  is_active: boolean | string | null;
};

type LinkedAccountRow = {
  user_id: string;
  account_status: string;
};

type LinkTokenRow = {
  user_id: string;
  is_expired: boolean;
  is_consumed: boolean;
};

type DeliveryStats = {
  attempted: number;
  delivered: number;
  transientFailures: string[];
};

const DRAIN_QUEUE_CRON = "*/15 * * * *";
const DEADLINE_REMINDERS_CRON = "0 1 * * *";
const ACTIVITY_WARNINGS_CRON = "0 9 * * FRI";
const CLAIM_STALE_AFTER = "1 hour";
const BOT_API_BASE = "https://api.telegram.org";
const PERMANENT_TELEGRAM_ERROR_SNIPPETS = [
  "bot was blocked by the user",
  "chat not found",
  "user is deactivated",
  "bot was kicked",
  "user is deleted",
];

const STATUS_LABELS: Record<string, string> = {
  admin: "管理員",
  active: "活躍成員",
  inactive: "非活躍成員",
};

const TOPIC_24H_SQL = `
SELECT
    tv.topic_text,
    tv.deadline_date,
    activity.telegram_chat_id
FROM ${TABLES.topicVotes} tv
CROSS JOIN ${TABLES.committeeVoteActivityView} activity
WHERE tv.status = 'pending'
  AND activity.telegram_chat_id IS NOT NULL
  AND tv.deadline_date = CURRENT_DATE + INTERVAL '1 day'
  AND NOT EXISTS (
      SELECT 1 FROM ${TABLES.topicVoteBallots} b
      WHERE b.topic_text = tv.topic_text
        AND b.user_id = activity.user_id
  )
`;

const DEPOSE_24H_SQL = `
SELECT
    tdv.topic_text,
    tdv.deadline_date,
    activity.telegram_chat_id
FROM ${TABLES.topicRemovalVotes} tdv
CROSS JOIN ${TABLES.committeeVoteActivityView} activity
WHERE tdv.status = 'pending'
  AND activity.telegram_chat_id IS NOT NULL
  AND tdv.deadline_date = CURRENT_DATE + INTERVAL '1 day'
  AND NOT EXISTS (
      SELECT 1 FROM ${TABLES.topicRemovalVoteBallots} b
      WHERE b.topic_text = tdv.topic_text
        AND b.user_id = activity.user_id
  )
`;

const ACTIVITY_WARNING_SQL = `
SELECT
    user_id,
    telegram_chat_id,
    account_status,
    participated_votes,
    total_votes,
    overall_rate_pct,
    last10_participated,
    is_active
FROM ${TABLES.committeeVoteActivityView}
WHERE total_votes > 0
  AND telegram_chat_id IS NOT NULL
  AND is_active = FALSE
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

export function isPrivateChat(message: TelegramMessage): boolean {
  return message.chat.type === "private";
}

export function normalizeLinkCode(value?: string | null): string {
  return String(value ?? "").toUpperCase().replace(/[^A-Z2-9]/g, "");
}

export async function hashTelegramLinkCode(code: string): Promise<string> {
  const normalized = normalizeLinkCode(code);
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(normalized));
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export function isPermanentTelegramError(message: string): boolean {
  const lower = message.toLowerCase();
  return PERMANENT_TELEGRAM_ERROR_SNIPPETS.some((snippet) => lower.includes(snippet));
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

async function getLinkedAccountByChatId(client: Client, tgChatId: string): Promise<LinkedAccountRow | null> {
  const result = await client.query<LinkedAccountRow>(
    `
    SELECT user_id, account_status
    FROM ${TABLES.accounts}
    WHERE telegram_chat_id = $1
      AND user_id NOT IN ('admin', 'developer', '')
    `,
    [tgChatId],
  );
  return (result.rowCount ?? 0) > 0 ? result.rows[0] : null;
}

async function requireLinkedPrivateAccount(
  client: Client,
  env: Env,
  message: TelegramMessage,
): Promise<LinkedAccountRow | null> {
  const tgChatId = String(message.chat.id);
  if (!isPrivateChat(message)) {
    await sendMessage(env, tgChatId, "請以 Telegram 私訊使用此指令。");
    return null;
  }

  const linkedAccount = await getLinkedAccountByChatId(client, tgChatId);
  if (!linkedAccount) {
    await sendMessage(
      env,
      tgChatId,
      "此 Telegram 帳戶未連結任何委員帳戶。\n請先到網站帳戶管理頁產生一次連結碼，再使用 /link <code> 進行連結。",
    );
    return null;
  }

  return linkedAccount;
}

export async function handleTelegramCommand(client: Client, env: Env, message: TelegramMessage): Promise<void> {
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
  const tgChatId = String(message.chat.id);
  if (!isPrivateChat(message)) {
    await sendMessage(env, tgChatId, "請以 Telegram 私訊使用 /link，群組或頻道不接受連結。");
    return;
  }

  if (args.length === 0) {
    await sendMessage(env, tgChatId, "用法：/link <一次連結碼>\n請先到網站帳戶管理頁產生連結碼。");
    return;
  }

  const normalizedCode = normalizeLinkCode(args[0]);
  if (!normalizedCode) {
    await sendMessage(env, tgChatId, "連結碼格式不正確。請返回網站重新產生後再試。");
    return;
  }

  const tokenHash = await hashTelegramLinkCode(normalizedCode);
  const tgUserId = String(message.from?.id ?? "");
  await client.query("BEGIN");
  try {
    const tokenResult = await client.query<LinkTokenRow>(
      `
      SELECT
          user_id,
          expires_at <= NOW() AS is_expired,
          consumed_at IS NOT NULL AS is_consumed
      FROM ${TABLES.telegramLinkTokens}
      WHERE token_hash = $1
      FOR UPDATE
      `,
      [tokenHash],
    );
    if ((tokenResult.rowCount ?? 0) === 0) {
      await client.query("ROLLBACK");
      await sendMessage(env, tgChatId, "連結碼無效。請返回網站重新產生後再試。");
      return;
    }

    const tokenRow = tokenResult.rows[0];
    if (tokenRow.is_consumed) {
      await client.query("ROLLBACK");
      await sendMessage(env, tgChatId, "此連結碼已被使用。請返回網站重新產生後再試。");
      return;
    }
    if (tokenRow.is_expired) {
      await client.query("ROLLBACK");
      await sendMessage(env, tgChatId, "此連結碼已過期。請返回網站重新產生後再試。");
      return;
    }

    const conflict = await client.query<{ user_id: string }>(
      `
      SELECT user_id
      FROM ${TABLES.accounts}
      WHERE (telegram_chat_id = $1 OR telegram_user_id = $2)
        AND user_id != $3
      LIMIT 1
      `,
      [tgChatId, tgUserId, tokenRow.user_id],
    );
    if ((conflict.rowCount ?? 0) > 0) {
      await client.query("ROLLBACK");
      await sendMessage(
        env,
        tgChatId,
        `此 Telegram 帳戶已連結至委員帳戶「${conflict.rows[0].user_id}」。\n請先發送 /unlink 解除連結後再試。`,
      );
      return;
    }

    const account = await client.query<{ account_status: string }>(
      `SELECT account_status FROM ${TABLES.accounts} WHERE user_id = $1`,
      [tokenRow.user_id],
    );
    if ((account.rowCount ?? 0) === 0) {
      await client.query("ROLLBACK");
      await sendMessage(env, tgChatId, "找不到對應的委員帳戶。請返回網站重新產生連結碼。");
      return;
    }

    await client.query(
      `UPDATE ${TABLES.accounts} SET telegram_user_id = $1, telegram_chat_id = $2 WHERE user_id = $3`,
      [tgUserId, tgChatId, tokenRow.user_id],
    );
    await client.query(
      `UPDATE ${TABLES.telegramLinkTokens} SET consumed_at = NOW() WHERE token_hash = $1`,
      [tokenHash],
    );
    await client.query("COMMIT");

    const accType = account.rows[0].account_status;
    const statusLabel = STATUS_LABELS[accType] ?? accType;
    await sendMessage(
      env,
      tgChatId,
      `✅ 連結成功！\n\n委員帳戶：${tokenRow.user_id}\n帳戶狀態：${statusLabel}\n\n你將會收到委員通知。\n前往投票：${buildVotePageUrl(env.APP_URL)}`,
    );
    return;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  }
}

async function cmdUnlink(client: Client, env: Env, message: TelegramMessage): Promise<void> {
  const linkedAccount = await requireLinkedPrivateAccount(client, env, message);
  if (!linkedAccount) {
    return;
  }

  await client.query(
    `UPDATE ${TABLES.accounts} SET telegram_user_id = NULL, telegram_chat_id = NULL WHERE user_id = $1`,
    [linkedAccount.user_id],
  );
  await sendMessage(env, String(message.chat.id), "已成功解除 Telegram 帳戶連結。");
}

async function cmdStatus(client: Client, env: Env, message: TelegramMessage): Promise<void> {
  const linkedAccount = await requireLinkedPrivateAccount(client, env, message);
  if (!linkedAccount) {
    return;
  }

  const statusLabel = STATUS_LABELS[linkedAccount.account_status] ?? linkedAccount.account_status;
  await sendMessage(env, String(message.chat.id), `已連結帳戶：${linkedAccount.user_id}\n帳戶狀態：${statusLabel}`);
}

async function cmdPending(client: Client, env: Env, message: TelegramMessage): Promise<void> {
  const linkedAccount = await requireLinkedPrivateAccount(client, env, message);
  if (!linkedAccount) {
    return;
  }

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
  const linkedAccount = await requireLinkedPrivateAccount(client, env, message);
  if (!linkedAccount) {
    return;
  }

  const stats = await client.query<ActivityRow>(
    `
    SELECT
        user_id,
        account_status,
        participated_votes,
        total_votes,
        overall_rate_pct,
        last10_participated,
        is_active
    FROM ${TABLES.committeeVoteActivityView}
    WHERE user_id = $1
    `,
    [linkedAccount.user_id],
  );

  const statRow = stats.rows[0];
  const total = toNumber(statRow?.total_votes);
  const participated = toNumber(statRow?.participated_votes);
  const last10 = toNumber(statRow?.last10_participated);
  const rate = total > 0 ? roundOneDecimal((participated / total) * 100) : 0;
  const accType = statRow?.account_status ?? linkedAccount.account_status;
  const statusLabel = STATUS_LABELS[accType] ?? accType;
  const rateOk = rate >= 40 ? "✅" : "⚠️";
  const last10Ok = last10 >= 3 ? "✅" : "⚠️";

  await sendHtml(
    env,
    String(message.chat.id),
    `<b>📊 個人投票紀錄 — ${escapeHtml(linkedAccount.user_id)}</b>\n\n` +
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
      "/link &lt;code&gt; — 使用網站產生的一次連結碼綁定帳戶（只限私訊）\n" +
      "/unlink — 解除 Telegram 連結（只限私訊）\n" +
      "/status — 查看連結狀態及帳戶類型（只限私訊）\n" +
      "/pending — 查看所有待表決的辯題及罷免動議（只限私訊）\n" +
      "/myvotes — 查看個人投票參與率（只限私訊）\n" +
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
      let delivery: DeliveryStats;
      switch (row.notification_type) {
        case "new_topic":
          delivery = await notifyNewTopicVote(client, env, payload as NewTopicPayload);
          break;
        case "new_depose":
          delivery = await notifyNewDeposeVote(client, env, payload as NewDeposePayload);
          break;
        case "vote_result":
          delivery = await notifyVoteResult(client, env, payload as VoteResultPayload);
          break;
        default:
          throw new Error(`Unknown notification_type '${row.notification_type}'`);
      }

      if (delivery.delivered > 0 || delivery.transientFailures.length === 0) {
        await markQueueRowProcessed(client, row.id, row.processing_token);
      } else {
        await releaseQueueRow(client, row.id, row.processing_token, delivery.transientFailures.join(" | "));
      }
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

async function getCommitteeAudienceChatIds(client: Client): Promise<string[]> {
  const rows = await client.query<{ telegram_chat_id: string }>(
    `
    SELECT telegram_chat_id
    FROM ${TABLES.committeeVoteActivityView}
    WHERE telegram_chat_id IS NOT NULL
    `,
  );
  return rows.rows.map((row) => row.telegram_chat_id);
}

async function clearTelegramLinkage(client: Client, chatId: string): Promise<void> {
  await client.query(
    `UPDATE ${TABLES.accounts} SET telegram_user_id = NULL, telegram_chat_id = NULL WHERE telegram_chat_id = $1`,
    [chatId],
  );
}

export async function deliverBroadcast(
  client: Client,
  env: Env,
  chatIds: string[],
  message: string,
): Promise<DeliveryStats> {
  const delivery: DeliveryStats = {
    attempted: chatIds.length,
    delivered: 0,
    transientFailures: [],
  };

  for (const chatId of chatIds) {
    try {
      await sendHtml(env, chatId, message, true);
      delivery.delivered += 1;
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      if (isPermanentTelegramError(errorMessage)) {
        await clearTelegramLinkage(client, chatId);
        console.error("Removed invalid Telegram linkage after permanent delivery failure", { chatId, error: errorMessage });
        continue;
      }

      delivery.transientFailures.push(`chat_id=${chatId}: ${errorMessage}`);
      console.error("Failed to deliver Telegram message", { chatId, error: errorMessage });
    }
  }

  return delivery;
}

async function notifyNewTopicVote(client: Client, env: Env, payload: NewTopicPayload): Promise<DeliveryStats> {
  const chatIds = await getCommitteeAudienceChatIds(client);

  const message =
    "<b>📋 新辯題待表決</b>\n\n" +
    `辯題：${escapeHtml(payload.topic)}\n` +
    `類別：${escapeHtml(payload.category)}　｜　${escapeHtml(payload.difficulty_label)}\n` +
    `提出者：${escapeHtml(payload.author)}\n` +
    `入庫門檻：${toNumber(payload.threshold)} 票　｜　截止：${escapeHtml(payload.deadline)} 23:59\n\n` +
    `<a href='${escapeHtml(buildVotePageUrl(env.APP_URL))}'>➡️ 立即前往投票</a>`;

  return deliverBroadcast(client, env, chatIds, message);
}

async function notifyNewDeposeVote(client: Client, env: Env, payload: NewDeposePayload): Promise<DeliveryStats> {
  const chatIds = await getCommitteeAudienceChatIds(client);

  const reasons = payload.reasons.length > 0 ? payload.reasons.map(escapeHtml).join("；") : "（未提供）";
  const message =
    "<b>⚠️ 新罷免動議</b>\n\n" +
    `辯題：${escapeHtml(payload.topic)}\n` +
    `提出者：${escapeHtml(payload.mover)}\n` +
    `罷免原因：${reasons}\n` +
    `罷免門檻：${toNumber(payload.threshold)} 票　｜　截止：${escapeHtml(payload.deadline)} 23:59\n\n` +
    `<a href='${escapeHtml(buildVotePageUrl(env.APP_URL))}'>➡️ 立即前往投票</a>`;

  return deliverBroadcast(client, env, chatIds, message);
}

async function notifyVoteResult(client: Client, env: Env, payload: VoteResultPayload): Promise<DeliveryStats> {
  const chatIds = await getCommitteeAudienceChatIds(client);
  return deliverBroadcast(client, env, chatIds, buildVoteResultMessage(buildVotePageUrl(env.APP_URL), payload));
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
    await deliverBroadcast(client, env, [row.telegram_chat_id], message);
  }

  for (const row of deposeRows.rows) {
    const message =
      "<b>⏰ 罷免投票截止提醒</b>\n\n" +
      `罷免動議「${escapeHtml(row.topic_text)}」將於明日截止，你尚未投票。\n` +
      `截止日期：${formatDate(row.deadline_date)} 23:59\n\n` +
      `<a href='${escapeHtml(buildVotePageUrl(env.APP_URL))}'>➡️ 立即前往投票</a>`;
    await deliverBroadcast(client, env, [row.telegram_chat_id], message);
  }
}

async function sendActivityWarnings(client: Client, env: Env): Promise<void> {
  const rows = await client.query<ActivityRow>(ACTIVITY_WARNING_SQL);

  for (const row of rows.rows) {
    const total = toNumber(row.total_votes);
    const participated = toNumber(row.participated_votes);
    const last10 = toNumber(row.last10_participated);
    const rate = toNumber(row.overall_rate_pct);
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
    await deliverBroadcast(client, env, [row.telegram_chat_id], message);
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
