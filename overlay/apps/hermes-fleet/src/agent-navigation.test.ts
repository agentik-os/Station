import { describe, expect, it } from "vitest";

import { agentManageRoute, agentSetupRoute } from "./agent-navigation.js";

describe("Fleet agent navigation", () => {
  it("opens the native Hermes profile builder inside the selected station", () => {
    expect(agentSetupRoute("agentik")).toBe("/agentik/profiles/new");
    expect(agentSetupRoute("private")).toBe("/private/profiles/new");
  });

  it("opens profile management with the canonical profile selected", () => {
    expect(agentManageRoute("mission", "collective")).toBe("/mission/profiles?profile=collective");
  });
});
