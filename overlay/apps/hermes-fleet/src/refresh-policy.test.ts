import { describe, expect, it } from "vitest";

import { acceptRefreshResult, renderTargetFor } from "./refresh-policy.js";

describe("Fleet refresh policy", () => {
  it("never rebuilds the shell for background or manual data refresh", () => {
    expect(renderTargetFor("timer", false)).toBe("content");
    expect(renderTargetFor("manual", false)).toBe("content");
    expect(renderTargetFor("data-loaded", false)).toBe("content");
  });

  it("rebuilds the shell only when navigation identity changes", () => {
    expect(renderTargetFor("organisation", false)).toBe("shell");
    expect(renderTargetFor("view", false)).toBe("content");
  });

  it("never replaces the live Hermes iframe during snapshot refresh", () => {
    expect(renderTargetFor("timer", true)).toBe("status");
    expect(renderTargetFor("manual", true)).toBe("status");
    expect(renderTargetFor("data-loaded", true)).toBe("status");
  });

  it("rejects stale or cross-organisation snapshot results", () => {
    expect(acceptRefreshResult(3, 3, "private", "private")).toBe(true);
    expect(acceptRefreshResult(2, 3, "private", "private")).toBe(false);
    expect(acceptRefreshResult(3, 3, "agentik", "private")).toBe(false);
  });
});
