import type { FleetStation, FleetTask } from "./fleet-snapshot.js";

export type FleetView = "overview" | "kanban" | "os" | "agents" | "sessions" | "hermes";

const STATUS_LABELS: Record<string, string> = {
  triage: "Triage",
  todo: "À faire",
  scheduled: "Planifié",
  ready: "Prêt",
  running: "En cours",
  review: "Revue",
  blocked: "Bloqué",
  done: "Terminé",
};

const DISCORD_STATUS_LABELS: Record<string, string> = {
  connected: "Connecté",
  configured: "Configuré",
  owner_required: "App/token requis",
  service_required: "Service requis",
  profile_required: "Profil bot requis",
};

const KANBAN_GROUPS = [
  { id: "queue", label: "File", statuses: ["triage", "todo", "scheduled", "ready"] },
  { id: "running", label: "En cours", statuses: ["running"] },
  { id: "review", label: "Revue", statuses: ["review"] },
  { id: "blocked", label: "Bloqué", statuses: ["blocked"] },
  { id: "done", label: "Terminé", statuses: ["done"] },
] as const;

export function escapeHtml(value: unknown): string {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function numberValue(record: Record<string, unknown>, key: string): number {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function textValue(record: Record<string, unknown>, key: string): string {
  return typeof record[key] === "string" ? String(record[key]) : "";
}

function recordValue(record: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = record[key];
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function stationBadge(station: FleetStation, global: boolean): string {
  return global ? `<span class="station-badge station-${escapeHtml(station.id)}">${escapeHtml(station.id)}</span>` : "";
}

function emptyState(message: string): string {
  return `<div class="empty-state"><span>◇</span><p>${escapeHtml(message)}</p></div>`;
}

export function renderStationOverview(stations: FleetStation[], global: boolean): string {
  const totals = stations.reduce(
    (sum, station) => ({
      sessions: sum.sessions + station.summary.active_sessions,
      runtimes: sum.runtimes + station.summary.active_runtimes,
      tasks: sum.tasks + station.summary.open_tasks,
      blocked: sum.blocked + station.summary.blocked_tasks,
      agents: sum.agents + station.summary.agent_count,
      os: sum.os + station.summary.os_count,
    }),
    { sessions: 0, runtimes: 0, tasks: 0, blocked: 0, agents: 0, os: 0 },
  );
  const cards = stations.map((station) => `
    <article class="station-card" data-station="${escapeHtml(station.id)}">
      <div class="station-card-head">
        <div><span class="health-dot ${station.healthy ? "is-online" : "is-offline"}"></span><strong>${escapeHtml(station.id)}</strong></div>
        <span>${station.healthy ? "Synchronisé" : "Indisponible"}</span>
      </div>
      <div class="station-metrics">
        <span><strong>${station.summary.open_tasks}</strong> tâches</span>
        <span><strong>${station.summary.active_sessions}</strong> sessions</span>
        <span><strong>${station.summary.active_runtimes}</strong> runtimes</span>
        <span><strong>${station.summary.agent_count}</strong> agents</span>
      </div>
      <div class="station-progress"><i style="width:${Math.min(100, station.summary.open_tasks ? ((station.kanban.counts.done ?? 0) / Math.max(1, station.summary.open_tasks + (station.kanban.counts.done ?? 0))) * 100 : 0)}%"></i></div>
      <footer><span>${escapeHtml(station.kanban.current_board)}</span><span>${station.summary.blocked_tasks ? `${station.summary.blocked_tasks} bloquée(s)` : "Aucun blocage"}</span></footer>
    </article>`).join("");

  const recentTasks = stations.flatMap((station) => station.kanban.tasks.map((task) => ({ station, task })))
    .sort((a, b) => b.task.created_at - a.task.created_at).slice(0, 8);
  const activity = recentTasks.length ? recentTasks.map(({ station, task }) => `
    <div class="activity-row">
      <span class="status-mark status-${escapeHtml(task.status)}"></span>
      <div><strong>${escapeHtml(task.title)}</strong><small>${escapeHtml(task.assignee || "non assigné")} · ${escapeHtml(task.board)}</small></div>
      ${stationBadge(station, global)}
      <time>${escapeHtml(STATUS_LABELS[task.status] ?? task.status)}</time>
    </div>`).join("") : emptyState("Aucune activité Kanban pour cette station.");

  return `
    <section class="page-heading"><div><span class="eyebrow">${global ? "Control plane global" : "Station active"}</span><h1>${global ? "Vue d’ensemble AGK" : `${escapeHtml(stations[0]?.id ?? "Station")} Station`}</h1><p>${global ? "État consolidé et redacted des quatre frontières AGK." : "Sessions, agents, OS et exécution dans une seule vue."}</p></div><div class="sync-chip"><span></span> Synchronisation automatique</div></section>
    <section class="kpi-strip">
      <article><span>Sessions actives</span><strong>${totals.sessions}</strong><small>${totals.sessions} sessions actives</small></article>
      <article><span>Runtimes actifs</span><strong>${totals.runtimes}</strong><small>AGK / RMUX</small></article>
      <article><span>Tâches ouvertes</span><strong>${totals.tasks}</strong><small>${totals.blocked} bloquée(s)</small></article>
      <article><span>Agents</span><strong>${totals.agents}</strong><small>${totals.agents} agents · ${totals.os} OS</small></article>
    </section>
    <section class="overview-grid"><div><div class="section-title"><h2>Stations</h2><span>${stations.length}</span></div><div class="station-grid">${cards}</div></div><div class="activity-panel"><div class="section-title"><h2>Activité récente</h2><span>Live</span></div>${activity}</div></section>`;
}

function renderTask(task: FleetTask, station: FleetStation, global: boolean): string {
  return `<article class="task-card priority-${Math.max(0, Math.min(3, task.priority))}">
    <div class="task-top">${stationBadge(station, global)}<span class="task-id">${escapeHtml(task.id.slice(0, 10))}</span><span class="priority-dot" title="Priorité ${task.priority}"></span></div>
    <h3>${escapeHtml(task.title)}</h3>
    <div class="task-meta"><span class="avatar">${escapeHtml((task.assignee || "?").slice(0, 1).toUpperCase())}</span><span>${escapeHtml(task.assignee || "Non assigné")}</span>${task.project_id ? `<span>· ${escapeHtml(task.project_id)}</span>` : ""}</div>
    <footer><span>${escapeHtml(task.board)}</span>${task.block_kind ? `<span class="block-kind">${escapeHtml(task.block_kind)}</span>` : ""}</footer>
  </article>`;
}

export function renderKanban(stations: FleetStation[], global: boolean): string {
  const taskRows = stations.flatMap((station) => station.kanban.tasks.map((task) => ({ station, task })));
  const columns = KANBAN_GROUPS.map((group) => {
    const tasks = taskRows.filter(({ task }) => group.statuses.includes(task.status as never));
    return `<section class="kanban-column column-${group.id}"><header><div><span class="column-dot"></span><strong>${group.label}</strong></div><span>${tasks.length}</span></header><div class="task-stack">${tasks.length ? tasks.map(({ station, task }) => renderTask(task, station, global)).join("") : emptyState("Aucune tâche")}</div></section>`;
  }).join("");
  return `<section class="page-heading compact"><div><span class="eyebrow">Exécution autonome</span><h1>Kanban ${global ? "Fleet" : escapeHtml(stations[0]?.id ?? "")}</h1><p>Cartes issues des boards Hermes actifs, synchronisées avec les sessions et workers.</p></div><div class="legend"><span><i class="status-running"></i>Actif</span><span><i class="status-blocked"></i>Bloqué</span></div></section><div class="kanban-board">${columns}</div>`;
}

export function renderOperatingSystems(stations: FleetStation[], global: boolean): string {
  const rows = stations.flatMap((station) => station.os.map((entry) => ({ station, entry })));
  return `<section class="page-heading compact"><div><span class="eyebrow">Operative systems</span><h1>OS Registry</h1><p>Packages installés, portée, capacités et agents responsables.</p></div></section><div class="data-table os-table"><div class="table-head"><span>OS</span><span>Station</span><span>Version</span><span>Composition</span><span>État</span></div>${rows.length ? rows.map(({ station, entry }) => {
    const installed = Boolean(entry.installed);
    const assigned = Boolean(entry.assigned);
    const state = !installed ? "À réparer" : assigned ? "Actif" : "Installé";
    const stateClass = !installed ? "is-warning" : assigned ? "is-ready" : "";
    return `<div class="table-row"><div><strong>${escapeHtml(textValue(entry, "name"))}</strong><small>${escapeHtml(textValue(entry, "description"))}</small></div><div>${stationBadge(station, global) || escapeHtml(station.id)}</div><code>${escapeHtml(textValue(entry, "version"))}</code><div class="composition"><span>${numberValue(entry, "skills")} skills</span><span>${numberValue(entry, "workflows")} flows</span><span>${numberValue(entry, "tools")} tools</span></div><span class="state-pill ${stateClass}">${state}</span></div>`;
  }).join("") : emptyState("Aucun Operative System autorisé pour cette station.")}</div>`;
}

export function renderAgents(stations: FleetStation[], global: boolean): string {
  const groups = stations.map((station) => {
    const cards = station.agents.map((agent) => {
      const name = textValue(agent, "name");
      const profile = textValue(agent, "profile") || "default";
      const discordState = recordValue(agent, "discord");
      const discordStatus = textValue(discordState, "status") || "profile_required";
      const dedicated = Boolean(discordState.dedicated) && profile !== "default";
      const botReady = Boolean(discordState.ready);
      const channelId = textValue(discordState, "channel_id");
      const applicationId = textValue(discordState, "application_id");
      const discordLabel = botReady ? "Prêt" : DISCORD_STATUS_LABELS[discordStatus] ?? discordStatus;
      const discordClass = botReady || discordStatus === "connected" ? "is-ready" : discordStatus === "configured" ? "" : "is-warning";
      const manage = profile !== "default"
        ? `<button type="button" class="agent-secondary" data-agent-manage="${escapeHtml(profile)}" data-agent-organisation="${escapeHtml(station.id)}">Profil</button>`
        : "";
      const discord = dedicated
        ? `<button type="button" class="agent-primary" data-agent-discord-configure="${escapeHtml(profile)}" data-agent-name="${escapeHtml(name)}" data-agent-application="${escapeHtml(applicationId)}" data-agent-channel="${escapeHtml(channelId)}" data-agent-organisation="${escapeHtml(station.id)}">Configurer</button><button type="button" class="agent-secondary" data-agent-discord="${escapeHtml(profile)}" data-agent-organisation="${escapeHtml(station.id)}">Hermes</button>`
        : `<button type="button" class="agent-primary" data-agent-bot-profile="${escapeHtml(textValue(agent, "id"))}" data-agent-organisation="${escapeHtml(station.id)}">Créer profil bot</button>`;
      const evidence = dedicated ? `<dl class="agent-evidence"><div><dt>Propriétaire</dt><dd class="${Boolean(discordState.owner_locked) ? "is-ok" : "is-missing"}">${Boolean(discordState.owner_locked) ? "Gareth verrouillé" : "À verrouiller"}</dd></div><div><dt>Canal</dt><dd class="${Boolean(discordState.channel_access) ? "is-ok" : "is-missing"}">${channelId ? `Canal ${escapeHtml(channelId)}` : "ID requis"}</dd></div><div><dt>Accès OS</dt><dd class="${Boolean(discordState.os_access) ? "is-ok" : "is-missing"}">${Boolean(discordState.os_access) ? "OS vérifié" : "Preuve requise"}</dd></div></dl>` : "";
      return `<article class="agent-card"><header><span class="agent-avatar">${escapeHtml(name.slice(0, 2).toUpperCase())}</span><div><strong>${escapeHtml(name)}</strong><small>${escapeHtml(textValue(agent, "id"))}</small></div><span class="state-pill ${discordClass}">${escapeHtml(discordLabel)}</span></header><p>${escapeHtml(textValue(agent, "description"))}</p>${evidence}<div class="agent-meta"><span>${escapeHtml(profile)}</span><code>${escapeHtml(textValue(discordState, "service") || `v${textValue(agent, "version")}`)}</code></div><footer>${manage}${discord}</footer></article>`;
    }).join("");
    return `<section class="agent-station" data-station="${escapeHtml(station.id)}"><header class="agent-station-header"><div><h2>${escapeHtml(station.id)}</h2><span>${station.agents.length} agent(s)</span></div><button type="button" class="setup-action" data-agent-setup="${escapeHtml(station.id)}">Nouvel agent</button></header><div class="agent-grid">${cards || emptyState("Aucun agent configuré pour cette station.")}</div></section>`;
  }).join("");
  return `<section class="page-heading compact"><div><span class="eyebrow">Workforce</span><h1>Agents</h1><p>Noms canoniques, profils Hermes et bindings OS. Créez et configurez un agent sans quitter Fleet.</p></div></section><div class="agent-stations">${groups || emptyState("Aucun agent autorisé.")}</div>`;
}

export function renderSessions(stations: FleetStation[], global: boolean): string {
  const rows = stations.flatMap((station) => station.sessions.map((session) => ({ station, session })))
    .sort((a, b) => numberValue(b.session, "last_activity_at") - numberValue(a.session, "last_activity_at"));
  return `<section class="page-heading compact"><div><span class="eyebrow">Hermes + AGK/RMUX</span><h1>Sessions</h1><p>Vue opérationnelle, sans contenu de conversation ni secret.</p></div></section><div class="data-table sessions-table"><div class="table-head"><span>Session</span><span>Station</span><span>Source</span><span>Modèle</span><span>Activité</span></div>${rows.length ? rows.map(({ station, session }) => `<div class="table-row"><div><strong>${escapeHtml(textValue(session, "title") || textValue(session, "id"))}</strong><small>${escapeHtml(textValue(session, "profile") || "default")}</small></div><div>${stationBadge(station, global) || escapeHtml(station.id)}</div><span>${escapeHtml(textValue(session, "source"))}</span><code>${escapeHtml(textValue(session, "model"))}</code><span class="state-pill ${session.active ? "is-ready" : ""}">${session.active ? "Active" : "Terminée"}</span></div>`).join("") : emptyState("Aucune session visible.")}</div>`;
}
