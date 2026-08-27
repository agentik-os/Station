export type FleetRenderReason =
  | "timer"
  | "manual"
  | "data-loaded"
  | "organisation"
  | "view";

export type FleetRenderTarget = "content" | "shell";

export function renderTargetFor(reason: FleetRenderReason): FleetRenderTarget {
  return reason === "organisation" ? "shell" : "content";
}
