import { describe, expect, it } from "vitest";

import { scopeFleetSnapshot, type FleetSnapshot } from "./fleet-snapshot.js";

const snapshot: FleetSnapshot = {
  schema: "agk.fleet.v1",
  generated_at: 100,
  organisations: {
    operator: {
      id: "operator", healthy: true, profiles: ["default"],
      summary: { active_sessions: 1, active_runtimes: 1, open_tasks: 1, blocked_tasks: 0, agent_count: 1, os_count: 1 },
      kanban: { current_board: "operator-station", boards: [], counts: { running: 1 }, tasks: [{ id: "op-task", board: "operator-station", title: "Global ops", assignee: "default", status: "running", priority: 1, created_at: 1, started_at: 1, completed_at: 0, session_id: "", project_id: "", block_kind: "" }] },
      sessions: [], runtimes: [], agents: [], os: [],
    },
    private: {
      id: "private", healthy: true, profiles: ["default", "client-secret"],
      summary: { active_sessions: 0, active_runtimes: 0, open_tasks: 1, blocked_tasks: 1, agent_count: 0, os_count: 0 },
      kanban: { current_board: "private-station", boards: [], counts: { blocked: 1 }, tasks: [{ id: "private-task", board: "private-station", title: "Private goal", assignee: "default", status: "blocked", priority: 0, created_at: 1, started_at: 0, completed_at: 0, session_id: "", project_id: "", block_kind: "human" }] },
      sessions: [], runtimes: [],
      agents: [{ id: "private-agent", name: "Private Agent", description: "Secret client assignment", profile: "client-secret", runtime: "hermes-profile", scope: ["private"], os: ["private-os"], ready: true }],
      os: [{ id: "private-os", name: "Private OS", version: "1.0.0", description: "Secret operating detail", scope: ["private"], agents: ["private-agent"], skills: 1, workflows: 1, tools: 1, installed: true, assigned: true }],
    },
  },
};

describe("Fleet snapshot scoping", () => {
  it("gives Operator the complete redacted station map", () => {
    const scoped = scopeFleetSnapshot(snapshot, "operator");
    expect(Object.keys(scoped.organisations)).toEqual(["operator", "private"]);
    expect(JSON.stringify(scoped)).not.toContain("Private goal");
    expect(JSON.stringify(scoped)).not.toContain("Secret client assignment");
    expect(JSON.stringify(scoped)).not.toContain("client-secret");
    expect(JSON.stringify(scoped)).not.toContain("private-agent");
    expect(JSON.stringify(scoped)).not.toContain("Secret operating detail");
    expect(scoped.organisations.private?.kanban.tasks[0]?.title).toBe("Task private-ta");
  });

  it("keeps non-Operator station responses focused and redacted", () => {
    const scoped = scopeFleetSnapshot(snapshot, "private");
    expect(Object.keys(scoped.organisations)).toEqual(["private"]);
    expect(JSON.stringify(scoped)).not.toContain("Global ops");
    expect(JSON.stringify(scoped)).not.toContain("Private goal");
    expect(JSON.stringify(scoped)).not.toContain("Secret client assignment");
    expect(JSON.stringify(scoped)).not.toContain("client-secret");
    expect(JSON.stringify(scoped)).not.toContain("private-agent");
    expect(JSON.stringify(scoped)).not.toContain("Secret operating detail");
    expect(scoped.organisations.private?.kanban.tasks[0]?.title).toBe("Task private-ta");
  });

  it("rejects unknown organisation scopes", () => {
    expect(() => scopeFleetSnapshot(snapshot, "unknown")).toThrow("Unknown organisation");
  });
});
