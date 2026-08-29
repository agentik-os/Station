import { dashboardPath, getOrganisation, type OrganisationId } from "./organisations.js";

export function agentSetupScope(organisation: OrganisationId, profile: string): {
  station: string;
  profile: string;
  prerequisite: string;
} {
  return {
    station: getOrganisation(organisation).label,
    profile,
    prerequisite: "Preuve OS requise avant Ready",
  };
}

export function agentDashboardRoute(
  organisation: OrganisationId,
  profile = "default",
): string {
  const params = new URLSearchParams({ profile });
  return `${dashboardPath(organisation)}?${params.toString()}`;
}

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

export function agentDiscordRoute(
  organisation: OrganisationId,
  profile: string,
): string {
  const params = new URLSearchParams({ profile });
  return `${dashboardPath(organisation)}channels?${params.toString()}`;
}
