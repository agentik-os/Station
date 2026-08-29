import { describe, expect, it } from "vitest";

import { agentDashboardRoute, agentDiscordRoute, agentManageRoute, agentSetupRoute, agentSetupScope } from "./agent-navigation.js";

describe("Fleet agent navigation", () => {
  it("opens the native Hermes profile builder inside the selected station", () => {
    expect(agentSetupRoute("agentik")).toBe("/agentik/profiles/new");
    expect(agentSetupRoute("private")).toBe("/private/profiles/new");
  });

  it("opens profile management with the canonical profile selected", () => {
    expect(agentManageRoute("mission", "collective")).toBe("/mission/profiles?profile=collective");
  });

  it("opens native Discord channel management for the exact profile", () => {
    expect(agentDiscordRoute("agentik", "brand-guardian")).toBe("/agentik/channels?profile=brand-guardian");
    expect(agentDiscordRoute("operator", "default")).toBe("/operator/channels?profile=default");
  });

  it("always scopes generic Hermes to the station default profile", () => {
    expect(agentDashboardRoute("private")).toBe("/private/?profile=default");
    expect(agentDashboardRoute("mission")).not.toContain("clientdentistry");
  });

  it("states the exact station, profile and OS prerequisite beside setup actions", () => {
    expect(agentSetupScope("private", "nutrition-os")).toEqual({
      station: "Private",
      profile: "nutrition-os",
      prerequisite: "Preuve OS requise avant Ready",
    });
  });
});
