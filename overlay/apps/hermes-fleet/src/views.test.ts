import { describe, expect, it } from "vitest";

import { renderKanban, renderStationOverview } from "./views.js";
import type { FleetStation } from "./fleet-snapshot.js";

const station: FleetStation = {
  id: "agentik",
  healthy: true,
  profiles: ["default", "builder"],
  summary: { active_sessions: 2, active_runtimes: 1, open_tasks: 2, blocked_tasks: 1, agent_count: 3, os_count: 2 },
  kanban: {
    current_board: "agentik-station",
    boards: [],
    counts: { running: 1, blocked: 1 },
    tasks: [
      { id: "t1", board: "agentik-station", title: "Ship <script>", assignee: "builder", status: "running", priority: 3, created_at: 1, started_at: 1, completed_at: 0, session_id: "", project_id: "", block_kind: "" },
      { id: "t2", board: "agentik-station", title: "Need owner", assignee: "default", status: "blocked", priority: 0, created_at: 1, started_at: 0, completed_at: 0, session_id: "", project_id: "", block_kind: "human" },
    ],
  },
  sessions: [], runtimes: [], agents: [], os: [],
};

describe("Fleet views", () => {
  it("renders dense station KPIs without unsafe HTML", () => {
    const html = renderStationOverview([station], false);
    expect(html).toContain("2 sessions actives");
    expect(html).toContain("3 agents");
    expect(html).not.toContain("<script>");
  });

  it("renders the operational Kanban columns and cards", () => {
    const html = renderKanban([station], false);
    expect(html).toContain("En cours");
    expect(html).toContain("Bloqué");
    expect(html).toContain("Ship &lt;script&gt;");
    expect(html).toContain("builder");
  });
});
