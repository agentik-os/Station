import { dashboardPath, type OrganisationId } from "./organisations.js";

export function agentSetupRoute(organisation: OrganisationId): string {
  return `${dashboardPath(organisation)}profiles/new`;
}

export function agentManageRoute(
  organisation: OrganisationId,
  profile: string,
): string {
  const params = new URLSearchParams({ profile });
  return `${dashboardPath(organisation)}profiles?${params.toString()}`;
}
