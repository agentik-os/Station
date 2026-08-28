export type FleetRenderReason =
  | "timer"
  | "manual"
  | "data-loaded"
  | "organisation"
  | "view";

export type FleetRenderTarget = "content" | "shell" | "status";

export function renderTargetFor(
  reason: FleetRenderReason,
  hermesActive: boolean,
): FleetRenderTarget {
  if (hermesActive && reason !== "organisation" && reason !== "view") return "status";
  return reason === "organisation" ? "shell" : "content";
}

export function acceptRefreshResult(
  requestGeneration: number,
  currentGeneration: number,
  requestedOrganisation: string,
  activeOrganisation: string,
): boolean {
  return requestGeneration === currentGeneration && requestedOrganisation === activeOrganisation;
}
