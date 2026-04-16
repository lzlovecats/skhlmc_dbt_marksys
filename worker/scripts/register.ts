const BOT_API_BASE = "https://api.telegram.org";

const BOT_COMMANDS = [
  { command: "link", description: "連結委員帳戶 /link <code>" },
  { command: "unlink", description: "解除 Telegram 連結" },
  { command: "status", description: "查看連結狀態" },
  { command: "pending", description: "查看所有待表決議案" },
  { command: "myvotes", description: "查看個人投票參與率" },
  { command: "help", description: "使用說明" },
];

async function main(): Promise<void> {
  const botToken = process.env.BOT_TOKEN;
  const workerBaseUrl = process.env.WORKER_BASE_URL?.replace(/\/$/, "");
  const webhookSecret = process.env.TELEGRAM_WEBHOOK_SECRET;

  if (!botToken || !workerBaseUrl || !webhookSecret) {
    throw new Error("BOT_TOKEN, WORKER_BASE_URL, and TELEGRAM_WEBHOOK_SECRET are required.");
  }

  const webhookUrl = `${workerBaseUrl}/telegram/${webhookSecret}`;

  await callTelegram(botToken, "setMyCommands", {
    commands: BOT_COMMANDS,
  });

  await callTelegram(botToken, "setWebhook", {
    url: webhookUrl,
    allowed_updates: ["message"],
  });

  console.log(`Webhook registered: ${webhookUrl}`);
}

async function callTelegram(
  botToken: string,
  method: string,
  payload: Record<string, unknown>,
): Promise<void> {
  const response = await fetch(`${BOT_API_BASE}/bot${botToken}/${method}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
    },
    body: JSON.stringify(payload),
  });

  const body = (await response.json()) as { ok: boolean; description?: string };
  if (!response.ok || !body.ok) {
    throw new Error(body.description ?? `Telegram API ${method} failed with status ${response.status}`);
  }
}

void main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
