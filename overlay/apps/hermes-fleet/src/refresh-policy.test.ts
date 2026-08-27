import { describe, expect, it } from "vitest";

import { renderTargetFor } from "./refresh-policy.js";

describe("Fleet refresh policy", () => {
  it("never rebuilds the shell for background or manual data refresh", () => {
    expect(renderTargetFor("timer")).toBe("content");
    expect(renderTargetFor("manual")).toBe("content");
    expect(renderTargetFor("data-loaded")).toBe("content");
  });

  it("rebuilds the shell only when navigation identity changes", () => {
    expect(renderTargetFor("organisation")).toBe("shell");
    expect(renderTargetFor("view")).toBe("content");
  });
});
