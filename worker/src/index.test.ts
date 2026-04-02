import { describe, expect, it } from "vitest";

import { buildPendingMessage, buildVotePageUrl, buildVoteResultMessage, normalizeCommand } from "./index";

describe("normalizeCommand", () => {
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
});

describe("buildPendingMessage", () => {
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
});

describe("buildVoteResultMessage", () => {
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
});
