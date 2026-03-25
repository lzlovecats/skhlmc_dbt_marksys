import { Client } from "pg";

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
  topic: string;
  deadline: string | Date;
  threshold: number | string;
  agree_count: number | string | null;
  against_count: number | string | null;
};

type QueueRow = {
  id: number;
  noti_type: string;
  payload: unknown;
  processing_token: string;
};

type ActivityWarningRow = {
  userid: string;
  tg_chatid: string;
  participated: number | string;
  total: number | string;
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

const CREATE_TG_NOTIFICATION_QUEUE_SQL = `
CREATE TABLE IF NOT EXISTS tg_notification_queue (
    id                      SERIAL      PRIMARY KEY,
    noti_type               TEXT        NOT NULL,
    payload                 JSONB       NOT NULL,
    created_at              TIMESTAMP   DEFAULT NOW(),
    processed               BOOLEAN     DEFAULT FALSE,
    processing_token        TEXT,
    processing_started_at   TIMESTAMP,
    last_error              TEXT
)
`;

const TG_NOTIFICATION_QUEUE_MIGRATIONS = [
  `
  ALTER TABLE tg_notification_queue
  ADD COLUMN IF NOT EXISTS processing_token TEXT
  `,
  `
  ALTER TABLE tg_notification_queue
  ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMP
  `,
  `
  ALTER TABLE tg_notification_queue
  ADD COLUMN IF NOT EXISTS last_error TEXT
  `,
  `
  CREATE INDEX IF NOT EXISTS idx_tg_notification_queue_claim
  ON tg_notification_queue (processed, processing_token, created_at)
  `,
];

const TOPIC_24H_SQL = `
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
`;

const DEPOSE_24H_SQL = `
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
`;

const ACTIVITY_WARNING_SQL = `
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
        `• ${escapeHtml(row.topic)}\n` +
          `  同意 ${toNumber(row.agree_count)} ／ 不同意 ${toNumber(row.against_count)}（門檻 ${toNumber(row.threshold)}）` +
          `  截止：${formatDate(row.deadline)}`,
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
        `• ${escapeHtml(row.topic)}\n` +
          `  同意 ${toNumber(row.agree_count)} ／ 不同意 ${toNumber(row.against_count)}（門檻 ${toNumber(row.threshold)}）` +
          `  截止：${formatDate(row.deadline)}`,
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

  const userid = args[0].trim();
  const tgChatId = String(message.chat.id);
  const tgUserId = String(message.from?.id ?? "");

  const account = await client.query<{ userid: string; acc_type: string }>(
    "SELECT userid, acc_type FROM accounts WHERE userid = $1",
    [userid],
  );
  if ((account.rowCount ?? 0) === 0) {
    await sendMessage(env, tgChatId, `找不到委員帳戶「${userid}」。請確認用戶名稱是否正確。`);
    return;
  }

  const existing = await client.query<{ userid: string }>(
    "SELECT userid FROM accounts WHERE tg_chatid = $1 AND userid != $2",
    [tgChatId, userid],
  );
  if ((existing.rowCount ?? 0) > 0) {
    await sendMessage(
      env,
      tgChatId,
      `此 Telegram 帳戶已連結至委員帳戶「${existing.rows[0].userid}」。\n請先發送 /unlink 解除連結後再試。`,
    );
    return;
  }

  await client.query(
    "UPDATE accounts SET tg_userid = $1, tg_chatid = $2 WHERE userid = $3",
    [tgUserId, tgChatId, userid],
  );

  const accType = account.rows[0].acc_type;
  const statusLabel = STATUS_LABELS[accType] ?? accType;
  await sendMessage(
    env,
    tgChatId,
    `✅ 連結成功！\n\n委員帳戶：${userid}\n帳戶狀態：${statusLabel}\n\n你將會收到辯題投票通知。\n前往投票：${buildVotePageUrl(env.APP_URL)}`,
  );
}

async function cmdUnlink(client: Client, env: Env, message: TelegramMessage): Promise<void> {
  const tgChatId = String(message.chat.id);
  const result = await client.query(
    "UPDATE accounts SET tg_userid = NULL, tg_chatid = NULL WHERE tg_chatid = $1",
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
  const result = await client.query<{ userid: string; acc_type: string }>(
    "SELECT userid, acc_type FROM accounts WHERE tg_chatid = $1",
    [tgChatId],
  );
  if ((result.rowCount ?? 0) === 0) {
    await sendMessage(env, tgChatId, "此 Telegram 帳戶未連結任何委員帳戶。\n使用 /link <userid> 進行連結。");
    return;
  }

  const row = result.rows[0];
  const statusLabel = STATUS_LABELS[row.acc_type] ?? row.acc_type;
  await sendMessage(env, tgChatId, `已連結帳戶：${row.userid}\n帳戶狀態：${statusLabel}`);
}

async function cmdPending(client: Client, env: Env, message: TelegramMessage): Promise<void> {
  const topicRows = await client.query<PendingRow>(`
    SELECT tv.topic, tv.deadline, tv.threshold,
           COUNT(CASE WHEN b.vote = 'agree' THEN 1 END)   AS agree_count,
           COUNT(CASE WHEN b.vote = 'against' THEN 1 END) AS against_count
    FROM topic_votes tv
    LEFT JOIN topic_vote_ballots b ON b.topic = tv.topic
    WHERE tv.status = 'pending'
    GROUP BY tv.topic, tv.deadline, tv.threshold
    ORDER BY tv.deadline ASC
  `);
  const deposeRows = await client.query<PendingRow>(`
    SELECT tdv.topic, tdv.deadline, tdv.threshold,
           COUNT(CASE WHEN b.vote = 'agree' THEN 1 END)   AS agree_count,
           COUNT(CASE WHEN b.vote = 'against' THEN 1 END) AS against_count
    FROM topic_depose_votes tdv
    LEFT JOIN depose_vote_ballots b ON b.topic = tdv.topic
    WHERE tdv.status = 'pending'
    GROUP BY tdv.topic, tdv.deadline, tdv.threshold
    ORDER BY tdv.deadline ASC
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
  const account = await client.query<{ userid: string; acc_type: string }>(
    "SELECT userid, acc_type FROM accounts WHERE tg_chatid = $1",
    [tgChatId],
  );
  if ((account.rowCount ?? 0) === 0) {
    await sendMessage(env, tgChatId, "此 Telegram 帳戶未連結任何委員帳戶。\n使用 /link <userid> 進行連結。");
    return;
  }

  const userid = account.rows[0].userid;
  const stats = await client.query<{
    total_votes: number | string | null;
    participated: number | string | null;
    last10_count: number | string | null;
  }>(
    `
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
    `,
    [userid],
  );

  const statRow = stats.rows[0];
  const total = toNumber(statRow.total_votes);
  const participated = toNumber(statRow.participated);
  const last10 = toNumber(statRow.last10_count);
  const rate = total > 0 ? roundOneDecimal((participated / total) * 100) : 0;
  const accType = account.rows[0].acc_type;
  const statusLabel = STATUS_LABELS[accType] ?? accType;
  const rateOk = rate >= 40 ? "✅" : "⚠️";
  const last10Ok = last10 >= 3 ? "✅" : "⚠️";

  await sendHtml(
    env,
    tgChatId,
    `<b>📊 個人投票紀錄 — ${escapeHtml(userid)}</b>\n\n` +
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
  await ensureQueueSchema(client);
  const processingToken = crypto.randomUUID();
  const rows = await claimQueueRows(client, processingToken);
  if (rows.length === 0) {
    return;
  }

  for (const row of rows) {
    const payload = typeof row.payload === "string" ? JSON.parse(row.payload) : row.payload;
    try {
      switch (row.noti_type) {
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
          throw new Error(`Unknown noti_type '${row.noti_type}'`);
      }

      await markQueueRowProcessed(client, row.id, row.processing_token);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      console.error("Failed to process queue row", { id: row.id, notiType: row.noti_type, error: message });
      await releaseQueueRow(client, row.id, row.processing_token, message);
    }
  }
}

async function ensureQueueSchema(client: Client): Promise<void> {
  await client.query(CREATE_TG_NOTIFICATION_QUEUE_SQL);
  for (const sql of TG_NOTIFICATION_QUEUE_MIGRATIONS) {
    await client.query(sql);
  }
}

async function claimQueueRows(client: Client, processingToken: string): Promise<QueueRow[]> {
  await client.query("BEGIN");
  try {
    const result = await client.query<QueueRow>(
      `
      WITH candidates AS (
          SELECT id
          FROM tg_notification_queue
          WHERE processed = FALSE
            AND (
                processing_token IS NULL
                OR processing_started_at < NOW() - INTERVAL '${CLAIM_STALE_AFTER}'
            )
          ORDER BY created_at ASC
          LIMIT 50
          FOR UPDATE SKIP LOCKED
      )
      UPDATE tg_notification_queue AS queue
      SET processing_token = $1,
          processing_started_at = NOW(),
          last_error = NULL
      FROM candidates
      WHERE queue.id = candidates.id
      RETURNING queue.id, queue.noti_type, queue.payload, queue.processing_token
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
    UPDATE tg_notification_queue
    SET processed = TRUE,
        processing_token = NULL,
        processing_started_at = NULL,
        last_error = NULL
    WHERE id = $1
      AND processing_token = $2
    `,
    [id, processingToken],
  );
}

async function releaseQueueRow(client: Client, id: number, processingToken: string, errorMessage: string): Promise<void> {
  await client.query(
    `
    UPDATE tg_notification_queue
    SET processing_token = NULL,
        processing_started_at = NULL,
        last_error = $3
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
  const rows = await client.query<{ tg_chatid: string }>(
    "SELECT tg_chatid FROM accounts WHERE acc_type = 'active' AND tg_chatid IS NOT NULL",
  );
  const chatIds = rows.rows.map((row) => row.tg_chatid);
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
  const rows = await client.query<{ tg_chatid: string }>(
    "SELECT tg_chatid FROM accounts WHERE acc_type = 'active' AND tg_chatid IS NOT NULL",
  );
  const chatIds = rows.rows.map((row) => row.tg_chatid);
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
  const rows = await client.query<{ tg_chatid: string }>(
    "SELECT tg_chatid FROM accounts WHERE tg_chatid IS NOT NULL",
  );
  const chatIds = rows.rows.map((row) => row.tg_chatid);
  if (chatIds.length === 0) {
    return;
  }

  await sendToUsersStrict(env, chatIds, buildVoteResultMessage(buildVotePageUrl(env.APP_URL), payload));
}

async function sendDeadlineReminders(client: Client, env: Env): Promise<void> {
  const topicRows = await client.query<{ topic: string; deadline: string | Date; tg_chatid: string }>(TOPIC_24H_SQL);
  const deposeRows = await client.query<{ topic: string; deadline: string | Date; tg_chatid: string }>(DEPOSE_24H_SQL);

  for (const row of topicRows.rows) {
    const message =
      "<b>⏰ 投票截止提醒</b>\n\n" +
      `辯題「${escapeHtml(row.topic)}」將於明日截止，你尚未投票。\n` +
      `截止日期：${formatDate(row.deadline)} 23:59\n\n` +
      `<a href='${escapeHtml(buildVotePageUrl(env.APP_URL))}'>➡️ 立即前往投票</a>`;
    await sendToUsersBestEffort(env, [row.tg_chatid], message);
  }

  for (const row of deposeRows.rows) {
    const message =
      "<b>⏰ 罷免投票截止提醒</b>\n\n" +
      `罷免動議「${escapeHtml(row.topic)}」將於明日截止，你尚未投票。\n` +
      `截止日期：${formatDate(row.deadline)} 23:59\n\n` +
      `<a href='${escapeHtml(buildVotePageUrl(env.APP_URL))}'>➡️ 立即前往投票</a>`;
    await sendToUsersBestEffort(env, [row.tg_chatid], message);
  }
}

async function sendActivityWarnings(client: Client, env: Env): Promise<void> {
  const rows = await client.query<ActivityWarningRow>(ACTIVITY_WARNING_SQL);

  for (const row of rows.rows) {
    const total = toNumber(row.total);
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
      `你好，${escapeHtml(row.userid)}！你的委員帳戶參與率未達標準：\n` +
      warnings.map((item) => `• ${escapeHtml(item)}`).join("\n") +
      "\n\n如未改善，帳戶將轉為非活躍狀態，屆時將不能提出新辯題或罷免動議。\n\n" +
      `<a href='${escapeHtml(buildVotePageUrl(env.APP_URL))}'>➡️ 立即前往投票</a>`;
    await sendToUsersBestEffort(env, [row.tg_chatid], message);
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
