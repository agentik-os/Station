import "./styles.css";

import {
  ORGANISATIONS,
  STORAGE_KEY,
  getOrganisation,
  resolveOrganisationId,
  withOrganisation,
  type OrganisationId,
} from "./organisations";
import {
  fetchFleetSnapshot,
  type FleetSnapshot,
  type FleetStation,
} from "./fleet-snapshot";
import {
  renderAgents,
  renderKanban,
  renderOperatingSystems,
  renderSessions,
  renderStationOverview,
  type FleetView,
} from "./views";
import {
  acceptRefreshResult,
  renderTargetFor,
  type FleetRenderReason,
} from "./refresh-policy";
import { agentDashboardRoute, agentDiscordRoute, agentManageRoute, agentSetupRoute } from "./agent-navigation";

const appElement = document.querySelector<HTMLDivElement>("#app");
if (!appElement) throw new Error("Application root is missing");
const app = appElement;

const VIEW_LABELS: ReadonlyArray<{ id: FleetView; label: string; glyph: string }> = [
  { id: "overview", label: "Overview", glyph: "◫" },
  { id: "kanban", label: "Kanban", glyph: "▦" },
  { id: "os", label: "OS", glyph: "◇" },
  { id: "agents", label: "Agents", glyph: "◎" },
  { id: "sessions", label: "Sessions", glyph: "⌁" },
  { id: "hermes", label: "Hermes", glyph: "›_" },
];

let activeId = resolveOrganisationId(
  window.location.search,
  window.localStorage.getItem(STORAGE_KEY),
);
let activeView: FleetView = "overview";
let hermesRoute = "";
let snapshot: FleetSnapshot | null = null;
let refreshTimer: number | null = null;
let refreshController: AbortController | null = null;
let refreshGeneration = 0;
let loading = true;
let errorMessage = "";

function organisationsForView(): FleetStation[] {
  if (!snapshot) return [];
  if (activeId === "operator") {
    return ORGANISATIONS.map(({ id }) => snapshot?.organisations[id]).filter(
      (station): station is FleetStation => Boolean(station),
    );
  }
  const station = snapshot.organisations[activeId];
  return station ? [station] : [];
}

function renderView(): string {
  const stations = organisationsForView();
  const global = activeId === "operator";
  if (loading && !snapshot) {
    return `<div class="loading-state"><span class="loading-orbit"></span><strong>Synchronisation de la Fleet</strong><small>Kanban · Sessions · Agents · Operative Systems</small></div>`;
  }
  if (errorMessage && !snapshot) {
    return `<div class="error-state"><span>!</span><strong>Snapshot indisponible</strong><p>${errorMessage}</p><button type="button" data-retry>Réessayer</button></div>`;
  }
  if (activeView === "hermes") {
    return `<section class="hermes-stage"><iframe title="Hermes ${activeId}" src="${hermesRoute || agentDashboardRoute(activeId)}"></iframe></section>`;
  }
  if (activeView === "kanban") return renderKanban(stations, global);
  if (activeView === "os") return renderOperatingSystems(stations, global);
  if (activeView === "agents") return renderAgents(stations, global);
  if (activeView === "sessions") return renderSessions(stations, global);
  return renderStationOverview(stations, global);
}

function shellTemplate(): string {
  const organisation = getOrganisation(activeId);
  return `
    <main class="fleet-shell" data-org="${activeId}">
      <aside class="sidebar">
        <a class="brand" href="${withOrganisation("", activeId)}" aria-label="AGK Fleet">
          <span class="brand-mark"><img src="/hermes-icon.webp" alt="" width="30" height="30"></span>
          <span><strong>AGK</strong><small>Fleet Control</small></span>
        </a>
        <nav class="primary-nav" aria-label="Navigation principale">
          ${VIEW_LABELS.map((view) => `<button type="button" data-view="${view.id}" class="${view.id === activeView ? "is-active" : ""}"><span>${view.glyph}</span>${view.label}</button>`).join("")}
        </nav>
        <div class="sidebar-stations">
          <span class="nav-caption">Stations</span>
          ${ORGANISATIONS.map((station) => `<button type="button" data-organisation="${station.id}" class="${station.id === activeId ? "is-active" : ""}"><i style="--station-color:${station.accent}"></i><span>${station.label}<small>${station.description}</small></span></button>`).join("")}
        </div>
        <footer class="sidebar-footer"><span class="tailnet-dot"></span><div><strong>Tailnet privé</strong><small>agk-core · chiffré</small></div></footer>
      </aside>
      <section class="workspace">
        <header class="workspace-bar">
          <div class="breadcrumb"><span>AGK</span><b>/</b><strong>${organisation.label}</strong><b>/</b><span>${VIEW_LABELS.find((view) => view.id === activeView)?.label ?? "Overview"}</span></div>
          <div class="workspace-actions">
            <span class="freshness" data-freshness>${snapshot ? "Synchronisé" : "Connexion…"}</span>
            <button type="button" class="icon-action" data-refresh title="Actualiser" aria-label="Actualiser">↻</button>
            <a class="open-action" href="${hermesRoute || agentDashboardRoute(activeId)}" target="_blank" rel="noopener noreferrer">Hermes ↗</a>
          </div>
        </header>
        ${activeId === "operator" ? `<div class="operator-banner"><span>Operator global</span><p>Vue redacted consolidée des quatre stations. Les prompts, messages, secrets et chemins privés ne quittent jamais leur frontière.</p></div>` : ""}
        <section class="content" data-content>${renderView()}</section>
      </section>
    </main>`;
}

function renderContent(): void {
  const content = app.querySelector<HTMLElement>("[data-content]");
  if (!content) {
    renderShell();
    return;
  }
  content.innerHTML = renderView();
  app.querySelector<HTMLElement>("[data-freshness]")!.textContent = errorMessage
    ? "Synchronisation dégradée"
    : "Synchronisé";
  app.querySelector("[data-refresh]")?.classList.remove("is-spinning");
  app.querySelector<HTMLButtonElement>("[data-retry]")?.addEventListener("click", () => {
    void refreshSnapshot("manual");
  });
  bindAgentControls();
}

function renderStatus(): void {
  app.querySelector<HTMLElement>("[data-freshness]")!.textContent = errorMessage
    ? "Synchronisation dégradée"
    : "Synchronisé";
  app.querySelector("[data-refresh]")?.classList.remove("is-spinning");
}

function selectView(view: FleetView): void {
  activeView = view;
  if (view === "hermes") hermesRoute = "";
  for (const button of app.querySelectorAll<HTMLButtonElement>("[data-view]")) {
    button.classList.toggle("is-active", button.dataset.view === activeView);
  }
  const breadcrumb = app.querySelector<HTMLElement>(".breadcrumb span:last-child");
  if (breadcrumb) {
    breadcrumb.textContent = VIEW_LABELS.find((item) => item.id === activeView)?.label ?? "Overview";
  }
  renderContent();
}

function openHermesRoute(organisation: OrganisationId, route: string): void {
  activeId = organisation;
  activeView = "hermes";
  hermesRoute = route;
  window.localStorage.setItem(STORAGE_KEY, organisation);
  window.history.replaceState({}, "", withOrganisation(window.location.search, organisation));
  renderShell();
}

function openDiscordSetup(button: HTMLButtonElement): void {
  const organisation = button.dataset.agentOrganisation as OrganisationId;
  const profile = button.dataset.agentDiscordConfigure ?? "";
  if (!profile || !ORGANISATIONS.some((item) => item.id === organisation)) return;
  app.querySelector("[data-discord-setup]")?.remove();
  const overlay = document.createElement("div");
  overlay.className = "setup-overlay";
  overlay.dataset.discordSetup = "";
  overlay.innerHTML = `<section class="setup-panel" role="dialog" aria-modal="true" aria-labelledby="discord-setup-title"><header><div><span class="eyebrow">Bot Discord dédié</span><h2 id="discord-setup-title"></h2></div><button type="button" class="setup-close" data-setup-close aria-label="Fermer">×</button></header><form data-discord-form><div class="setup-copy"><p>Un canal exact. Un seul propriétaire autorisé. Aucun token n’entre dans Fleet.</p></div><label>Profil<input name="profile" readonly></label><label>Utilisateur autorisé · verrouillé<input value="Gareth · 1441423462492016821" readonly></label><label>Application ID Discord<input name="application_id" inputmode="numeric" pattern="[0-9]{17,20}" minlength="17" maxlength="20" required placeholder="154…"></label><a class="discord-install-link" data-discord-install target="_blank" rel="noopener noreferrer" aria-disabled="true">Installer le bot dans Discord ↗</a><label>ID du canal Discord<input name="channel_id" inputmode="numeric" pattern="[0-9]{17,20}" minlength="17" maxlength="20" required placeholder="154…"></label><div class="setup-checks"><span>Canal dédié</span><span>Allow-user verrouillé</span><span>Preuve OS requise avant Ready</span></div><p class="setup-result" data-setup-result></p><footer><button type="button" class="agent-secondary" data-setup-close>Annuler</button><button type="button" class="agent-secondary" data-secure-token>Token sécurisé</button><button type="submit" class="agent-primary">Enregistrer le routage</button></footer></form></section>`;
  overlay.querySelector<HTMLElement>("h2")!.textContent = button.dataset.agentName ?? profile;
  overlay.querySelector<HTMLInputElement>('input[name="profile"]')!.value = profile;
  const applicationInput = overlay.querySelector<HTMLInputElement>('input[name="application_id"]')!;
  const installLink = overlay.querySelector<HTMLAnchorElement>("[data-discord-install]")!;
  applicationInput.value = button.dataset.agentApplication ?? "";
  const updateInstallLink = () => {
    const value = applicationInput.value.trim();
    const valid = /^\d{17,20}$/.test(value);
    installLink.toggleAttribute("aria-disabled", !valid);
    if (valid) installLink.href = `https://discord.com/oauth2/authorize?client_id=${encodeURIComponent(value)}&scope=bot%20applications.commands&permissions=274877975552`;
    else installLink.removeAttribute("href");
  };
  applicationInput.addEventListener("input", updateInstallLink);
  updateInstallLink();
  const channelInput = overlay.querySelector<HTMLInputElement>('input[name="channel_id"]')!;
  channelInput.value = button.dataset.agentChannel ?? "";
  const shell = app.querySelector<HTMLElement>(".fleet-shell");
  if (shell) shell.inert = true;
  const close = () => {
    document.removeEventListener("keydown", onKeyDown);
    if (shell) shell.inert = false;
    overlay.remove();
    button.focus();
  };
  const onKeyDown = (event: KeyboardEvent) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = [...overlay.querySelectorAll<HTMLElement>('button:not([disabled]), input:not([disabled]), [href], [tabindex]:not([tabindex="-1"])')];
    if (!focusable.length) return;
    const first = focusable[0]!;
    const last = focusable[focusable.length - 1]!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus();
    }
  };
  document.addEventListener("keydown", onKeyDown);
  for (const control of overlay.querySelectorAll<HTMLButtonElement>("[data-setup-close]")) control.addEventListener("click", close);
  overlay.addEventListener("click", (event) => { if (event.target === overlay) close(); });
  overlay.querySelector<HTMLButtonElement>("[data-secure-token]")!.addEventListener("click", async (event) => {
    const control = event.currentTarget as HTMLButtonElement;
    const result = overlay.querySelector<HTMLElement>("[data-setup-result]")!;
    if (!applicationInput.checkValidity() || !channelInput.checkValidity()) {
      result.textContent = "Application ID et Channel ID valides requis.";
      result.className = "setup-result is-error";
      return;
    }
    control.disabled = true;
    result.textContent = "Création du lien Tailnet sécurisé…";
    result.className = "setup-result";
    try {
      const start = await fetch("/api/agent-discord/secure-input", { method: "POST", headers: { "content-type": "application/json", accept: "application/json" }, body: JSON.stringify({ organisation, profile, application_id: applicationInput.value.trim(), channel_id: channelInput.value.trim() }) });
      const started = await start.json() as { error?: string; request_id?: string };
      if (!start.ok || !started.request_id) throw new Error(started.error || `Échec (${start.status})`);
      let readyUrl = "";
      for (let attempt = 0; attempt < 40 && !readyUrl; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 500));
        const status = await fetch(`/api/agent-discord/secure-input?id=${encodeURIComponent(started.request_id)}`, { headers: { accept: "application/json" }, cache: "no-store" });
        const state = await status.json() as { url?: string; error?: string };
        if (status.ok && state.url) readyUrl = state.url;
        else if (status.status >= 400 && status.status !== 503) throw new Error(state.error || `Échec (${status.status})`);
      }
      if (!readyUrl) throw new Error("Le lien sécurisé n’est pas encore disponible.");
      result.textContent = "";
      const link = document.createElement("a");
      link.href = readyUrl; link.target = "_blank"; link.rel = "noopener noreferrer";
      link.textContent = "Ouvrir le champ token sécurisé ↗";
      result.appendChild(link);
      result.className = "setup-result is-ok";
    } catch (error) {
      result.textContent = error instanceof Error ? error.message : String(error);
      result.className = "setup-result is-error";
      control.disabled = false;
    }
  });
  overlay.querySelector<HTMLFormElement>("[data-discord-form]")!.addEventListener("submit", async (event) => {
    event.preventDefault();
    const input = overlay.querySelector<HTMLInputElement>('input[name="channel_id"]')!;
    const result = overlay.querySelector<HTMLElement>("[data-setup-result]")!;
    const submit = overlay.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    submit.disabled = true; result.textContent = "Vérification et écriture…";
    try {
      const response = await fetch("/api/agent-discord/setup", { method: "POST", headers: { "content-type": "application/json", accept: "application/json" }, body: JSON.stringify({ organisation, profile, application_id: applicationInput.value.trim(), channel_id: input.value.trim() }) });
      const payload = await response.json() as { error?: string };
      if (!response.ok) throw new Error(payload.error || `Échec (${response.status})`);
      result.textContent = "Demande sécurisée envoyée. Application puis preuve E2E requises avant Ready.";
      result.classList.add("is-ok");
      window.setTimeout(() => { close(); void refreshSnapshot("manual"); }, 900);
    } catch (error) {
      result.textContent = error instanceof Error ? error.message : String(error);
      result.classList.add("is-error"); submit.disabled = false;
    }
  });
  app.appendChild(overlay);
  window.setTimeout(() => overlay.querySelector<HTMLInputElement>('input[name="channel_id"]')?.focus(), 0);
}

function bindAgentControls(): void {
  for (const button of app.querySelectorAll<HTMLButtonElement>("[data-agent-setup]")) {
    button.addEventListener("click", () => {
      const id = button.dataset.agentSetup as OrganisationId;
      if (ORGANISATIONS.some((item) => item.id === id)) {
        openHermesRoute(id, agentSetupRoute(id));
      }
    });
  }
  for (const button of app.querySelectorAll<HTMLButtonElement>("[data-agent-manage]")) {
    button.addEventListener("click", () => {
      const id = button.dataset.agentOrganisation as OrganisationId;
      const profile = button.dataset.agentManage ?? "";
      if (profile && ORGANISATIONS.some((item) => item.id === id)) {
        openHermesRoute(id, agentManageRoute(id, profile));
      }
    });
  }
  for (const button of app.querySelectorAll<HTMLButtonElement>("[data-agent-discord]")) {
    button.addEventListener("click", () => {
      const id = button.dataset.agentOrganisation as OrganisationId;
      const profile = button.dataset.agentDiscord ?? "default";
      if (ORGANISATIONS.some((item) => item.id === id)) {
        openHermesRoute(id, agentDiscordRoute(id, profile));
      }
    });
  }
  for (const button of app.querySelectorAll<HTMLButtonElement>("[data-agent-discord-configure]")) {
    button.addEventListener("click", () => openDiscordSetup(button));
  }
  for (const button of app.querySelectorAll<HTMLButtonElement>("[data-agent-bot-profile]")) {
    button.addEventListener("click", () => {
      const id = button.dataset.agentOrganisation as OrganisationId;
      if (ORGANISATIONS.some((item) => item.id === id)) {
        openHermesRoute(id, agentSetupRoute(id));
      }
    });
  }
}

function bindInteractions(): void {
  for (const button of app.querySelectorAll<HTMLButtonElement>("[data-view]")) {
    button.addEventListener("click", () => selectView(button.dataset.view as FleetView));
  }
  for (const button of app.querySelectorAll<HTMLButtonElement>("[data-organisation]")) {
    button.addEventListener("click", () => {
      const id = button.dataset.organisation as OrganisationId;
      if (!ORGANISATIONS.some((item) => item.id === id)) return;
      activeId = id;
      hermesRoute = "";
      snapshot = null;
      errorMessage = "";
      loading = true;
      window.localStorage.setItem(STORAGE_KEY, id);
      window.history.replaceState({}, "", withOrganisation(window.location.search, id));
      renderShell();
      void refreshSnapshot("organisation");
    });
  }
  bindAgentControls();
  app.querySelector<HTMLButtonElement>("[data-refresh]")?.addEventListener("click", () => {
    void refreshSnapshot("manual");
  });
}

function renderShell(): void {
  app.innerHTML = shellTemplate();
  bindInteractions();
}

async function refreshSnapshot(reason: FleetRenderReason): Promise<void> {
  const generation = ++refreshGeneration;
  const requestedOrganisation = activeId;
  refreshController?.abort();
  const controller = new AbortController();
  refreshController = controller;
  loading = true;
  errorMessage = "";
  app.querySelector("[data-refresh]")?.classList.add("is-spinning");
  try {
    const nextSnapshot = await fetchFleetSnapshot(requestedOrganisation, controller.signal);
    if (!acceptRefreshResult(generation, refreshGeneration, requestedOrganisation, activeId)) return;
    snapshot = nextSnapshot;
  } catch (error) {
    if (controller.signal.aborted || generation !== refreshGeneration) return;
    errorMessage = error instanceof Error ? error.message : String(error);
  } finally {
    if (!acceptRefreshResult(generation, refreshGeneration, requestedOrganisation, activeId)) return;
    if (refreshController === controller) refreshController = null;
    loading = false;
    const target = renderTargetFor(reason, activeView === "hermes");
    if (target === "shell") renderShell();
    else if (target === "content") renderContent();
    else renderStatus();
  }
}

function startRefreshLoop(): void {
  if (refreshTimer !== null) window.clearInterval(refreshTimer);
  refreshTimer = window.setInterval(() => void refreshSnapshot("timer"), 30_000);
}

renderShell();
void refreshSnapshot("data-loaded");
startRefreshLoop();
