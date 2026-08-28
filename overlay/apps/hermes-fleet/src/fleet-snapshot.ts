import { isOrganisationId, type OrganisationId } from "./organisations.js";

export interface FleetTask {
  id: string;
  board: string;
  title: string;
  assignee: string;
  status: string;
  priority: number;
  created_at: number;
  started_at: number;
  completed_at: number;
  session_id: string;
  project_id: string;
  block_kind: string;
}

export interface FleetBoard {
  slug: string;
  name: string;
  description: string;
  icon: string;
  color: string;
  current: boolean;
  counts: Record<string, number>;
  task_count: number;
}

export interface FleetStation {
  id: string;
  healthy: boolean;
  profiles: string[];
  summary: {
    active_sessions: number;
    active_runtimes: number;
    open_tasks: number;
    blocked_tasks: number;
    agent_count: number;
    os_count: number;
  };
  kanban: {
    current_board: string;
    boards: FleetBoard[];
    counts: Record<string, number>;
    tasks: FleetTask[];
  };
  sessions: Array<Record<string, unknown>>;
  runtimes: Array<Record<string, unknown>>;
  agents: Array<Record<string, unknown>>;
  os: Array<Record<string, unknown>>;
}

export interface FleetSnapshot {
  schema: "agk.fleet.v1";
  generated_at: number;
  organisations: Partial<Record<OrganisationId, FleetStation>> &
    Record<string, FleetStation | undefined>;
}

function redactCrossBoundaryStation(station: FleetStation): FleetStation {
  return {
    ...station,
    profiles: (station.profiles ?? []).map((profile, index) =>
      profile === "default" ? "default" : `specialist-${String(index).padStart(2, "0")}`,
    ),
    kanban: {
      ...station.kanban,
      tasks: (station.kanban.tasks ?? []).map((task) => ({
        ...task,
        title: `Task ${String(task.id ?? "unknown").slice(0, 10)}`,
        assignee: task.assignee === "default" ? "default" : "specialist",
        session_id: "",
        project_id: "",
      })),
    },
    sessions: (station.sessions ?? []).map((session) => ({
      ...session,
      title: `Session ${String(session.id ?? "").slice(0, 10)}`,
      profile: session.profile === "default" ? "default" : "specialist",
    })),
    runtimes: (station.runtimes ?? []).map((runtime) => ({
      ...runtime,
      name: `Runtime ${String(runtime.id ?? "").slice(0, 10)}`,
      profile: "",
    })),
    agents: (station.agents ?? []).map((agent, index) => ({
      id: agent.runtime === "hermes-profile" ? `specialist-${String(index + 1).padStart(2, "0")}` : agent.id,
      name: agent.runtime === "hermes-profile" ? `Specialist ${String(index + 1).padStart(2, "0")}` : agent.name,
      version: agent.version,
      runtime: agent.runtime,
      ready: agent.ready,
      description: "Spécialiste Hermes isolé.",
      profile: "",
      scope: [],
      os: [],
    })),
    os: (station.os ?? []).map((operatingSystem) => ({
      id: operatingSystem.id,
      name: operatingSystem.name,
      version: operatingSystem.version,
      installed: operatingSystem.installed,
      assigned: operatingSystem.assigned,
      skills: operatingSystem.skills,
      workflows: operatingSystem.workflows,
      tools: operatingSystem.tools,
      description: "Operative System AGK vérifié.",
      scope: [],
      agents: [],
    })),
  };
}

export function scopeFleetSnapshot(
  snapshot: FleetSnapshot,
  organisation: string,
): FleetSnapshot {
  if (!isOrganisationId(organisation)) {
    throw new Error(`Unknown organisation: ${organisation}`);
  }
  if (organisation === "operator") {
    const organisations = Object.fromEntries(
      Object.entries(snapshot.organisations).map(([id, station]) => [
        id,
        station ? redactCrossBoundaryStation(station) : station,
      ]),
    );
    return { ...snapshot, organisations };
  }
  const station = snapshot.organisations[organisation];
  return {
    ...snapshot,
    organisations: station ? { [organisation]: redactCrossBoundaryStation(station) } : {},
  };
}

export async function fetchFleetSnapshot(
  organisation: OrganisationId,
  signal?: AbortSignal,
): Promise<FleetSnapshot> {
  const response = await fetch(
    `/api/fleet-snapshot?org=${encodeURIComponent(organisation)}`,
    { headers: { accept: "application/json" }, cache: "no-store", signal },
  );
  if (!response.ok) {
    throw new Error(`Fleet snapshot request failed (${response.status})`);
  }
  return (await response.json()) as FleetSnapshot;
}
