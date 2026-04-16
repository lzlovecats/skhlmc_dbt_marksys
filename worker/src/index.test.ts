import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  buildPendingMessage,
  buildVotePageUrl,
  buildVoteResultMessage,
  deliverBroadcast,
  handleTelegramCommand,
  hashTelegramLinkCode,
  isPermanentTelegramError,
  normalizeCommand,
  normalizeLinkCode,
} from "./index";

function createMockClient(handler: (sql: string, params: unknown[] | undefined) => { rows?: unknown[]; rowCount?: number }) {
  return {
    query: vi.fn(async (sql: string, params?: unknown[]) => {
      const result = handler(sql, params);
      return {
        rows: result.rows ?? [],
        rowCount: result.rowCount ?? (result.rows ? result.rows.length : 0),
      };
    }),
  };
}

const env = {
  APP_URL: "https://example.com",
  BOT_TOKEN: "test-token",
  TELEGRAM_WEBHOOK_SECRET: "secret",
  HYPERDRIVE: { connectionString: "postgres://example" },
};

describe("telegram worker helpers", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  function stubTelegramOk(): void {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ ok: true, result: {} }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
  }

  it("parses plain commands", () => {
    expect(normalizeCommand("/link user123")).toEqual({
      command: "/link",
      args: ["user123"],
    });
  });

  it("strips bot mentions", () => {
    expect(normalizeCommand("/pending@skhlmc_bot")).toEqual({
      command: "/pending",
      args: [],
    });
  });

  it("returns null for non commands", () => {
    expect(normalizeCommand("hello")).toBeNull();
  });

  it("renders both pending sections", () => {
    const text = buildPendingMessage(
      [
        {
          topic_text: "Example Topic",
          deadline_date: "2026-03-30",
          approval_threshold: 5,
          agree_count: 2,
          against_count: 1,
        },
      ],
      [],
      buildVotePageUrl("https://example.com"),
    );

    expect(text).toContain("Example Topic");
    expect(text).toContain("辯題入庫投票");
    expect(text).toContain("目前沒有待表決的罷免動議。");
    expect(text).toContain("https://example.com/vote");
  });

  it("renders topic result copy", () => {
    const text = buildVoteResultMessage("https://example.com/vote", {
      topic: "Topic A",
      result: "passed",
      vote_type: "topic",
      agree_count: 4,
      against_count: 1,
      threshold: 3,
    });

    expect(text).toContain("辯題已通過入庫");
    expect(text).toContain("Topic A");
    expect(text).toContain("門檻：3");
  });

  it("normalizes and hashes link codes consistently", async () => {
    expect(normalizeLinkCode("ab cd-2345")).toBe("ABCD2345");
    expect(await hashTelegramLinkCode("abcd-2345")).toBe(await hashTelegramLinkCode("ABCD2345"));
  });

  it("rejects /link outside private chats", async () => {
    stubTelegramOk();
    const client = createMockClient(() => ({ rows: [] }));

    await handleTelegramCommand(client as never, env, {
      chat: { id: "1001", type: "group" },
      from: { id: "2002" },
      text: "/link ABCD-EFGH-IJKL",
    });

    const payload = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(payload.text).toContain("請以 Telegram 私訊使用 /link");
    expect(client.query).not.toHaveBeenCalled();
  });

  it("links a private chat with a valid one-time code", async () => {
    stubTelegramOk();
    const tokenHash = await hashTelegramLinkCode("ABCD-EFGH-IJKL");
    const client = createMockClient((sql, params) => {
      if (sql === "BEGIN" || sql === "COMMIT") {
        return { rows: [] };
      }
      if (sql.includes("FROM telegram_link_tokens")) {
        expect(params).toEqual([tokenHash]);
        return { rows: [{ user_id: "alice", is_expired: false, is_consumed: false }] };
      }
      if (sql.includes("WHERE (telegram_chat_id = $1 OR telegram_user_id = $2)")) {
        return { rows: [] };
      }
      if (sql.includes("SELECT account_status FROM accounts")) {
        return { rows: [{ account_status: "active" }] };
      }
      if (sql.includes("UPDATE accounts SET telegram_user_id = $1, telegram_chat_id = $2")) {
        expect(params).toEqual(["2002", "1001", "alice"]);
        return { rowCount: 1 };
      }
      if (sql.includes("UPDATE telegram_link_tokens SET consumed_at = NOW()")) {
        expect(params).toEqual([tokenHash]);
        return { rowCount: 1 };
      }
      throw new Error(`Unexpected SQL: ${sql}`);
    });

    await handleTelegramCommand(client as never, env, {
      chat: { id: "1001", type: "private" },
      from: { id: "2002" },
      text: "/link ABCD-EFGH-IJKL",
    });

    const payload = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(payload.text).toContain("連結成功");
    expect(payload.text).toContain("alice");
  });

  it("rejects expired link codes", async () => {
    stubTelegramOk();
    const client = createMockClient((sql) => {
      if (sql === "BEGIN" || sql === "ROLLBACK") {
        return { rows: [] };
      }
      if (sql.includes("FROM telegram_link_tokens")) {
        return { rows: [{ user_id: "alice", is_expired: true, is_consumed: false }] };
      }
      throw new Error(`Unexpected SQL: ${sql}`);
    });

    await handleTelegramCommand(client as never, env, {
      chat: { id: "1001", type: "private" },
      from: { id: "2002" },
      text: "/link ABCD-EFGH-IJKL",
    });

    const payload = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(payload.text).toContain("已過期");
  });

  it("rejects /pending for unlinked chats", async () => {
    stubTelegramOk();
    const client = createMockClient((sql, params) => {
      if (sql.includes("FROM accounts") && sql.includes("telegram_chat_id = $1")) {
        expect(params).toEqual(["1001"]);
        return { rows: [] };
      }
      throw new Error(`Unexpected SQL: ${sql}`);
    });

    await handleTelegramCommand(client as never, env, {
      chat: { id: "1001", type: "private" },
      from: { id: "2002" },
      text: "/pending",
    });

    const payload = JSON.parse(String(fetchMock.mock.calls[0][1]?.body));
    expect(payload.text).toContain("未連結任何委員帳戶");
  });

  it("treats blocked-chat errors as permanent and does not create transient failures", async () => {
    fetchMock
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ok: true, result: {} }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ok: false, description: "Forbidden: bot was blocked by the user" }), {
          status: 403,
          headers: { "content-type": "application/json" },
        }),
      );
    const client = createMockClient((sql, params) => {
      if (sql.includes("SET telegram_user_id = NULL, telegram_chat_id = NULL")) {
        expect(params).toEqual(["chat-2"]);
        return { rowCount: 1 };
      }
      throw new Error(`Unexpected SQL: ${sql}`);
    });

    const delivery = await deliverBroadcast(client as never, env, ["chat-1", "chat-2"], "<b>hello</b>");

    expect(delivery.delivered).toBe(1);
    expect(delivery.transientFailures).toEqual([]);
    expect(isPermanentTelegramError("Forbidden: bot was blocked by the user")).toBe(true);
  });
});
