import "./styles.css";

import {
  ORGANISATIONS,
  STORAGE_KEY,
  dashboardPath,
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
import { renderTargetFor, type FleetRenderReason } from "./refresh-policy";

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
let snapshot: FleetSnapshot | null = null;
let refreshTimer: number | null = null;
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
    return `<section class="hermes-stage"><iframe title="Hermes ${activeId}" src="${dashboardPath(activeId)}"></iframe></section>`;
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
            <a class="open-action" href="${dashboardPath(activeId)}" target="_blank" rel="noopener noreferrer">Hermes ↗</a>
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
}

function selectView(view: FleetView): void {
  activeView = view;
  for (const button of app.querySelectorAll<HTMLButtonElement>("[data-view]")) {
    button.classList.toggle("is-active", button.dataset.view === activeView);
  }
  const breadcrumb = app.querySelector<HTMLElement>(".breadcrumb span:last-child");
  if (breadcrumb) {
    breadcrumb.textContent = VIEW_LABELS.find((item) => item.id === activeView)?.label ?? "Overview";
  }
  renderContent();
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
      snapshot = null;
      errorMessage = "";
      loading = true;
      window.localStorage.setItem(STORAGE_KEY, id);
      window.history.replaceState({}, "", withOrganisation(window.location.search, id));
      renderShell();
      void refreshSnapshot("organisation");
    });
  }
  app.querySelector<HTMLButtonElement>("[data-refresh]")?.addEventListener("click", () => {
    void refreshSnapshot("manual");
  });
}

function renderShell(): void {
  app.innerHTML = shellTemplate();
  bindInteractions();
}

async function refreshSnapshot(reason: FleetRenderReason): Promise<void> {
  loading = true;
  errorMessage = "";
  app.querySelector("[data-refresh]")?.classList.add("is-spinning");
  try {
    snapshot = await fetchFleetSnapshot(activeId);
  } catch (error) {
    errorMessage = error instanceof Error ? error.message : String(error);
  } finally {
    loading = false;
    if (renderTargetFor(reason) === "shell") renderShell();
    else renderContent();
  }
}

function startRefreshLoop(): void {
  if (refreshTimer !== null) window.clearInterval(refreshTimer);
  refreshTimer = window.setInterval(() => void refreshSnapshot("timer"), 30_000);
}

renderShell();
void refreshSnapshot("data-loaded");
startRefreshLoop();
