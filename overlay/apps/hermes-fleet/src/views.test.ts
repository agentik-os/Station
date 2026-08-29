import { describe, expect, it } from "vitest";

import { renderAgents, renderKanban, renderSessions, renderStationOverview } from "./views.js";
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

  it("renders canonical agent names with setup and management controls", () => {
    const withAgents: FleetStation = {
      ...station,
      agents: [{ id: "agk-architect", name: "AGK Architect", description: "Designs AGK systems", profile: "agk-architect", version: "profile", runtime: "hermes-profile", ready: true, discord: { dedicated: true, status: "connected", token_configured: true, service_installed: true, gateway_connected: true, channel_id: "1542137541572956193", application_id: "1542135948475637861", owner_locked: true, channel_access: true, os_access: true, e2e_verified: true, ready: true } }],
    };
    const html = renderAgents([withAgents], false);
    expect(html).toContain("AGK Architect");
    expect(html).not.toContain("Specialist 01");
    expect(html).toContain('data-agent-setup="agentik"');
    expect(html).toContain('data-agent-manage="agk-architect"');
    expect(html).toContain('data-agent-discord="agk-architect"');
    expect(html).toContain("Configurer");
    expect(html).toContain("Prêt");
    expect(html).toContain("Canal 1542137541572956193");
    expect(html).toContain("OS vérifié");
    expect(html).toContain('data-agent-discord-configure="agk-architect"');
    expect(html).toContain('data-agent-application="1542135948475637861"');
  });

  it("renders canonical session titles and profile names", () => {
    const withSessions: FleetStation = {
      ...station,
      sessions: [{ id: "session-1", title: "Brand review", profile: "brand-guardian", source: "discord", model: "gpt-5.6-sol", last_activity_at: 2, active: true }],
    };
    const html = renderSessions([withSessions], false);
    expect(html).toContain("Brand review");
    expect(html).toContain("brand-guardian");
    expect(html).not.toContain("Session session-1");
  });

  it("requires a dedicated profile before offering a bot to an unprofiled agent", () => {
    const unprofiled: FleetStation = {
      ...station,
      agents: [{ id: "completion-oracle", name: "Completion Oracle", description: "Verifies completion", profile: "", version: "1.0.0", runtime: "hermes", ready: true, discord: { dedicated: false, status: "profile_required", token_configured: false, service_installed: false, gateway_connected: false } }],
    };
    const html = renderAgents([unprofiled], false);
    expect(html).toContain("Profil bot requis");
    expect(html).toContain('data-agent-bot-profile="completion-oracle"');
    expect(html).not.toContain('data-agent-discord="default"');
  });
});
