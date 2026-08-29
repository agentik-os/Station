import { chmodSync, createReadStream, realpathSync, unlinkSync } from "node:fs";
import { mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import { createServer, request as createUpstreamRequest } from "node:http";
import type {
  IncomingHttpHeaders,
  IncomingMessage,
  OutgoingHttpHeaders,
  Server,
  ServerResponse,
} from "node:http";
import { extname, relative, resolve, sep } from "node:path";
import type { Duplex } from "node:stream";
import { fileURLToPath } from "node:url";

import {
  ORGANISATIONS,
  isOrganisationId,
  type OrganisationId,
} from "../src/organisations.js";
import {
  scopeFleetSnapshot,
  type FleetSnapshot,
} from "../src/fleet-snapshot.js";

export const FLEET_LISTEN_HOST = "127.0.0.1";
export const DEFAULT_FLEET_PORT = 8459;

const LOOPBACK_HOSTS = ["localhost", "127.0.0.1"] as const;
const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

const CONTENT_TYPES: Readonly<Record<string, string>> = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".ico": "image/x-icon",
  ".jpeg": "image/jpeg",
  ".jpg": "image/jpeg",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

interface UpstreamTarget {
  id: OrganisationId;
  host: typeof FLEET_LISTEN_HOST;
  port: number;
  prefix: string;
}

export interface FleetServerOptions {
  allowedHosts?: readonly string[] | string;
  distDir?: string;
  upstreamPorts?: Partial<Record<OrganisationId, number>>;
  onProxyError?: (error: Error, target: OrganisationId) => void;
  snapshotPath?: string;
  operatorLogins?: readonly string[] | string;
  discordOwnerId?: string;
  routingRequestDir?: string;
  allowMutationOverTcpForTests?: boolean;
  secureStatusDir?: string;
  discordGuildId?: string;
}

interface RequestAuthorization {
  allowed: boolean;
  status: number;
  message: string;
}

function canonicalHostname(value: string): string | null {
  const candidate = value.trim();
  if (!candidate || candidate.includes("/") || candidate.includes("@")) {
    return null;
  }

  try {
    const url = new URL(`http://${candidate}`);
    return url.hostname.toLowerCase().replace(/\.$/, "");
  } catch {
    return null;
  }
}

export function parseAllowedHosts(
  configured: readonly string[] | string | undefined =
    process.env.HERMES_FLEET_ALLOWED_HOSTS,
): ReadonlySet<string> {
  const values = Array.isArray(configured)
    ? configured
    : typeof configured === "string"
      ? configured.split(",")
      : [];
  const allowed = new Set<string>(LOOPBACK_HOSTS);

  for (const value of values) {
    const hostname = canonicalHostname(value);
    if (!hostname) {
      throw new Error(`Invalid HERMES_FLEET_ALLOWED_HOSTS entry: ${value}`);
    }
    allowed.add(hostname);
  }

  return allowed;
}

function originHostname(origin: string): string | null {
  try {
    const parsed = new URL(origin);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return null;
    }
    return parsed.hostname.toLowerCase().replace(/\.$/, "");
  } catch {
    return null;
  }
}

function authorizeRequest(
  request: IncomingMessage,
  allowedHosts: ReadonlySet<string>,
): RequestAuthorization {
  const host = request.headers.host;
  const hostname = host ? canonicalHostname(host) : null;
  if (!hostname || !allowedHosts.has(hostname)) {
    return { allowed: false, status: 421, message: "Host is not allowed" };
  }

  const origin = request.headers.origin;
  if (origin) {
    const hostnameFromOrigin = originHostname(origin);
    const tailscaleProxyOriginAllowed =
      Boolean(request.headers["tailscale-user-login"]) &&
      LOOPBACK_HOSTS.includes(hostname as (typeof LOOPBACK_HOSTS)[number]) &&
      Boolean(hostnameFromOrigin && allowedHosts.has(hostnameFromOrigin));
    if (
      !hostnameFromOrigin ||
      !allowedHosts.has(hostnameFromOrigin) ||
      (hostnameFromOrigin !== hostname && !tailscaleProxyOriginAllowed)
    ) {
      return { allowed: false, status: 403, message: "Origin is not allowed" };
    }
  }

  return { allowed: true, status: 200, message: "OK" };
}

function makeUpstreams(
  portOverrides: FleetServerOptions["upstreamPorts"] = {},
): readonly UpstreamTarget[] {
  return ORGANISATIONS.map((organisation) => ({
    id: organisation.id,
    host: FLEET_LISTEN_HOST,
    port: portOverrides[organisation.id] ?? organisation.port,
    prefix: organisation.path,
  }));
}

function targetForPath(
  pathname: string,
  upstreams: readonly UpstreamTarget[],
): UpstreamTarget | null {
  return (
    upstreams.find((target) => {
      const withoutTrailingSlash = target.prefix.slice(0, -1);
      return pathname === withoutTrailingSlash || pathname.startsWith(target.prefix);
    }) ?? null
  );
}

function targetForUnprefixedAsset(
  request: IncomingMessage,
  pathname: string,
  upstreams: readonly UpstreamTarget[],
): UpstreamTarget | null {
  if (!pathname.startsWith("/assets/")) {
    return null;
  }

  const referer = request.headers.referer;
  const requestHost = request.headers.host;
  if (!referer || !requestHost) {
    return null;
  }

  try {
    const refererUrl = new URL(referer);
    if (refererUrl.hostname !== canonicalHostname(requestHost)) {
      return null;
    }
    return targetForPath(refererUrl.pathname, upstreams);
  } catch {
    return null;
  }
}

function upstreamPath(requestUrl: string, target: UpstreamTarget): string {
  const parsed = new URL(requestUrl, "http://fleet.invalid");
  const prefixWithoutTrailingSlash = target.prefix.slice(0, -1);
  const stripped = parsed.pathname.slice(prefixWithoutTrailingSlash.length);
  return `${stripped || "/"}${parsed.search}`;
}

function cloneProxyHeaders(
  source: IncomingHttpHeaders,
  request: IncomingMessage,
  target: UpstreamTarget,
  websocket: boolean,
): OutgoingHttpHeaders {
  const headers: OutgoingHttpHeaders = {};

  for (const [name, value] of Object.entries(source)) {
    if (!HOP_BY_HOP_HEADERS.has(name) && value !== undefined) {
      headers[name] = value;
    }
  }

  const upstreamAuthority = `${target.host}:${target.port}`;
  headers.host = upstreamAuthority;
  headers["x-forwarded-host"] = request.headers.host ?? "";
  headers["x-forwarded-prefix"] = target.prefix.slice(0, -1);
  headers["x-forwarded-proto"] = "https";

  if (request.headers.origin) {
    headers.origin = `http://${upstreamAuthority}`;
  }

  if (websocket) {
    headers.connection = "Upgrade";
    headers.upgrade = request.headers.upgrade ?? "websocket";
  }

  return headers;
}

function rewriteLocation(value: string, target: UpstreamTarget): string {
  if (value.startsWith("/") && !value.startsWith("//")) {
    return `${target.prefix.slice(0, -1)}${value}`;
  }

  try {
    const location = new URL(value);
    if (
      location.hostname === target.host &&
      Number(location.port || (location.protocol === "https:" ? 443 : 80)) ===
        target.port
    ) {
      return `${target.prefix.slice(0, -1)}${location.pathname}${location.search}${location.hash}`;
    }
  } catch {
    // Relative redirect values already stay below the proxied prefix.
  }

  return value;
}

function rewriteSetCookie(value: string, target: UpstreamTarget): string {
  const prefix = target.prefix;
  if (/;\s*Path=/i.test(value)) {
    return value.replace(
      /;\s*Path=(\/[^;]*)/i,
      (_match, upstreamCookiePath: string) =>
        `; Path=${prefix.slice(0, -1)}${upstreamCookiePath}`,
    );
  }
  return `${value}; Path=${prefix}`;
}

function responseHeaders(
  headers: IncomingHttpHeaders,
  target: UpstreamTarget,
): OutgoingHttpHeaders {
  const result: OutgoingHttpHeaders = {};

  for (const [name, value] of Object.entries(headers)) {
    if (HOP_BY_HOP_HEADERS.has(name) || value === undefined) {
      continue;
    }
    if (name === "location" && typeof value === "string") {
      result[name] = rewriteLocation(value, target);
    } else if (name === "set-cookie") {
      const cookies = Array.isArray(value) ? value : [value];
      result[name] = cookies.map((cookie) => rewriteSetCookie(cookie, target));
    } else {
      result[name] = value;
    }
  }

  return result;
}

function sendText(
  response: ServerResponse,
  status: number,
  message: string,
): void {
  const body = `${message}\n`;
  response.writeHead(status, {
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(body),
    "content-type": "text/plain; charset=utf-8",
    "x-content-type-options": "nosniff",
  });
  response.end(body);
}

function sendJson(response: ServerResponse, status: number, payload: unknown): void {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "cache-control": "no-store",
    "content-length": Buffer.byteLength(body),
    "content-type": "application/json; charset=utf-8",
    "x-content-type-options": "nosniff",
  });
  response.end(body);
}

function ownerLogin(request: IncomingMessage, operatorLogins: ReadonlySet<string>): boolean {
  const header = request.headers["tailscale-user-login"];
  const login = (Array.isArray(header) ? header[0] : header)?.trim().toLowerCase();
  return Boolean(login && operatorLogins.has(login));
}

async function readJsonBody(request: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > 16_384) throw new Error("Request body too large");
    chunks.push(buffer);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("JSON object required");
  return value as Record<string, unknown>;
}

async function queueRequest(requestDir: string, payload: Record<string, unknown>): Promise<string> {
  await mkdir(requestDir, { recursive: true, mode: 0o700 });
  const requestId = `${Date.now()}-${process.pid}-${Math.random().toString(16).slice(2, 14)}`;
  const temporary = resolve(requestDir, `.${requestId}.tmp`);
  const target = resolve(requestDir, `${requestId}.json`);
  await writeFile(temporary, JSON.stringify(payload) + "\n", { encoding: "utf8", mode: 0o600, flag: "wx" });
  await rename(temporary, target);
  return requestId;
}

async function setupAgentDiscord(request: IncomingMessage, response: ServerResponse, snapshotPath: string, requestDir: string, operatorLogins: ReadonlySet<string>, discordOwnerId: string): Promise<void> {
  if (!ownerLogin(request, operatorLogins)) return sendJson(response, 403, { error: "Owner identity required" });
  try {
    const body = await readJsonBody(request);
    const organisation = String(body.organisation ?? "");
    const profile = String(body.profile ?? "");
    const channelId = String(body.channel_id ?? "");
    const applicationId = String(body.application_id ?? "");
    if (!isOrganisationId(organisation) || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(profile) || !/^\d{17,20}$/.test(channelId) || !/^\d{17,20}$/.test(applicationId)) return sendJson(response, 400, { error: "Valid organisation, profile, application ID and Discord channel ID required" });
    if (!/^\d{17,20}$/.test(discordOwnerId)) return sendJson(response, 503, { error: "Discord owner ID is not configured" });
    const snapshot = JSON.parse(await readFile(snapshotPath, "utf8")) as FleetSnapshot;
    const station = snapshot.organisations[organisation];
    if (!station?.agents?.some((agent) => agent.profile === profile)) return sendJson(response, 404, { error: "Agent profile is not registered in this station" });
    const requestId = await queueRequest(requestDir, { schema: "agk.agent-discord-routing.v1", organisation, profile, application_id: applicationId, channel_id: channelId, owner_id: discordOwnerId });
    sendJson(response, 200, { ok: true, queued: true, request_id: requestId, organisation, profile, application_id: applicationId, channel_id: channelId, owner_id: discordOwnerId, restart_required: true, ready: false });
  } catch (error) {
    console.error(`[hermes-fleet] Discord routing setup failed: ${error instanceof Error ? error.message : String(error)}`);
    sendJson(response, 502, { error: "Discord routing setup failed safely" });
  }
}

async function startAgentSecureInput(request: IncomingMessage, response: ServerResponse, snapshotPath: string, requestDir: string, operatorLogins: ReadonlySet<string>, discordOwnerId: string, discordGuildId: string): Promise<void> {
  if (!ownerLogin(request, operatorLogins)) return sendJson(response, 403, { error: "Owner identity required" });
  try {
    const body = await readJsonBody(request);
    const organisation = String(body.organisation ?? "");
    const profile = String(body.profile ?? "");
    const applicationId = String(body.application_id ?? "");
    const channelId = String(body.channel_id ?? "");
    if (!isOrganisationId(organisation) || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(profile) || !/^\d{17,20}$/.test(applicationId) || !/^\d{17,20}$/.test(channelId) || !/^\d{17,20}$/.test(discordGuildId)) return sendJson(response, 400, { error: "Valid station, profile, application, guild and channel required" });
    const snapshot = JSON.parse(await readFile(snapshotPath, "utf8")) as FleetSnapshot;
    const station = snapshot.organisations[organisation];
    const agent = station?.agents?.find((candidate) => candidate.profile === profile);
    if (!agent) return sendJson(response, 404, { error: "Agent profile is not registered in this station" });
    const declaredOs = Array.isArray(agent.os) && agent.os.length ? String(agent.os[0]).split("@", 1)[0] : profile;
    const operatingSystem = station?.os?.find((candidate) => candidate.id === declaredOs && candidate.installed === true);
    const osVersion = typeof operatingSystem?.version === "string" ? operatingSystem.version : "";
    if (!operatingSystem || !/^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/.test(osVersion)) return sendJson(response, 409, { error: "Installed OS package evidence required before bot onboarding" });
    const requestId = await queueRequest(requestDir, { schema: "agk.agent-discord-secure-input.v1", organisation, profile, application_id: applicationId, channel_id: channelId, guild_id: discordGuildId, owner_id: discordOwnerId, expected_os_id: declaredOs, expected_os_version: osVersion });
    sendJson(response, 200, { ok: true, queued: true, request_id: requestId, status: "starting" });
  } catch (error) {
    console.error(`[hermes-fleet] Secure Input launch failed: ${error instanceof Error ? error.message : String(error)}`);
    sendJson(response, 502, { error: "Secure Input launch failed safely" });
  }
}

async function serveSecureInputStatus(request: IncomingMessage, response: ServerResponse, statusDir: string, operatorLogins: ReadonlySet<string>, requestId: string | null): Promise<void> {
  if (!ownerLogin(request, operatorLogins)) return sendJson(response, 403, { error: "Owner identity required" });
  if (!requestId || !/^\d+-\d+-[0-9a-f]{1,24}$/.test(requestId)) return sendJson(response, 400, { error: "Invalid request ID" });
  try {
    const text = await readFile(resolve(statusDir, `${requestId}.jsonl`), "utf8");
    const ready = text.split(/\r?\n/).map((line) => { try { return JSON.parse(line); } catch { return null; } }).find((row) => row?.status === "READY");
    if (!ready || typeof ready.url !== "string" || !ready.url.startsWith("https://")) return sendJson(response, 202, { status: "starting" });
    sendJson(response, 200, { status: "ready", url: ready.url, expires_in_seconds: ready.expires_in_seconds, transport: ready.transport });
  } catch (error) {
    if (error instanceof Error && "code" in error && error.code === "ENOENT") return sendJson(response, 202, { status: "starting" });
    sendJson(response, 503, { error: "Secure Input status unavailable" });
  }
}

async function serveFleetSnapshot(
  request: IncomingMessage,
  response: ServerResponse,
  snapshotPath: string,
  organisation: string | null,
  operatorLogins: ReadonlySet<string>,
): Promise<void> {
  if (!isOrganisationId(organisation)) {
    sendJson(response, 400, { error: "Unknown organisation" });
    return;
  }
  const loginHeader = request.headers["tailscale-user-login"];
  const login = (Array.isArray(loginHeader) ? loginHeader[0] : loginHeader)
    ?.trim().toLowerCase();
  if (!login || !operatorLogins.has(login)) {
    sendJson(response, 403, { error: "Owner identity required" });
    return;
  }
  try {
    const payload = JSON.parse(await readFile(snapshotPath, "utf8")) as FleetSnapshot;
    if (payload.schema !== "agk.fleet.v1" || typeof payload.organisations !== "object") {
      throw new Error("Invalid Fleet snapshot schema");
    }
    sendJson(response, 200, scopeFleetSnapshot(payload, organisation));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(`[hermes-fleet] snapshot error: ${message}`);
    sendJson(response, 503, { error: "Fleet snapshot unavailable" });
  }
}

function proxyHttp(
  request: IncomingMessage,
  response: ServerResponse,
  target: UpstreamTarget,
  onProxyError: NonNullable<FleetServerOptions["onProxyError"]>,
  pathOverride?: string,
): void {
  const proxyRequest = createUpstreamRequest(
    {
      host: target.host,
      port: target.port,
      method: request.method,
      path: pathOverride ?? upstreamPath(request.url ?? "/", target),
      headers: cloneProxyHeaders(request.headers, request, target, false),
      agent: false,
    },
    (proxyResponse) => {
      response.writeHead(
        proxyResponse.statusCode ?? 502,
        proxyResponse.statusMessage,
        responseHeaders(proxyResponse.headers, target),
      );
      proxyResponse.on("error", (error) => response.destroy(error));
      proxyResponse.pipe(response);
    },
  );

  proxyRequest.on("error", (error) => {
    onProxyError(error, target.id);
    if (!response.headersSent) {
      sendText(response, 502, "Hermes dashboard is unavailable");
    } else {
      response.destroy(error);
    }
  });

  request.on("aborted", () => proxyRequest.destroy());
  request.on("error", (error) => proxyRequest.destroy(error));
  request.pipe(proxyRequest);
}

function contentType(filePath: string): string {
  return CONTENT_TYPES[extname(filePath).toLowerCase()] ?? "application/octet-stream";
}

async function serveStatic(
  request: IncomingMessage,
  response: ServerResponse,
  distDir: string,
): Promise<void> {
  const parsed = new URL(request.url ?? "/", "http://fleet.invalid");
  let decodedPath: string;
  try {
    decodedPath = decodeURIComponent(parsed.pathname);
  } catch {
    sendText(response, 400, "Malformed request path");
    return;
  }

  const relativePath = decodedPath === "/" ? "index.html" : decodedPath.replace(/^\/+/, "");
  let filePath = resolve(distDir, relativePath);
  const relativeToRoot = relative(distDir, filePath);
  if (relativeToRoot.startsWith(`..${sep}`) || relativeToRoot === "..") {
    sendText(response, 403, "Path is outside the Fleet application");
    return;
  }

  let fileStats;
  try {
    fileStats = await stat(filePath);
    if (fileStats.isDirectory()) {
      filePath = resolve(filePath, "index.html");
      fileStats = await stat(filePath);
    }
  } catch {
    if (request.headers.accept?.includes("text/html")) {
      filePath = resolve(distDir, "index.html");
      try {
        fileStats = await stat(filePath);
      } catch {
        sendText(response, 503, "Fleet frontend is not built");
        return;
      }
    } else {
      sendText(response, 404, "Not found");
      return;
    }
  }

  if (!fileStats.isFile()) {
    sendText(response, 404, "Not found");
    return;
  }

  response.writeHead(200, {
    "cache-control": filePath.endsWith("index.html")
      ? "no-cache"
      : "public, max-age=31536000, immutable",
    "content-length": fileStats.size,
    "content-type": contentType(filePath),
    "x-content-type-options": "nosniff",
  });

  if (request.method === "HEAD") {
    response.end();
    return;
  }

  const stream = createReadStream(filePath);
  stream.on("error", (error) => response.destroy(error));
  stream.pipe(response);
}

function rejectUpgrade(socket: Duplex, status: number, message: string): void {
  if (!socket.writable) {
    socket.destroy();
    return;
  }
  const body = `${message}\n`;
  socket.end(
    `HTTP/1.1 ${status} ${message}\r\n` +
      "Connection: close\r\n" +
      "Content-Type: text/plain; charset=utf-8\r\n" +
      `Content-Length: ${Buffer.byteLength(body)}\r\n\r\n${body}`,
  );
}

function writeUpgradeResponse(
  socket: Duplex,
  response: IncomingMessage,
): void {
  socket.write(
    `HTTP/1.1 ${response.statusCode ?? 101} ${response.statusMessage ?? "Switching Protocols"}\r\n`,
  );
  for (let index = 0; index < response.rawHeaders.length; index += 2) {
    const name = response.rawHeaders[index];
    const value = response.rawHeaders[index + 1];
    if (name && value) {
      socket.write(`${name}: ${value}\r\n`);
    }
  }
  socket.write("\r\n");
}

function proxyWebSocket(
  request: IncomingMessage,
  clientSocket: Duplex,
  head: Buffer,
  target: UpstreamTarget,
  onProxyError: NonNullable<FleetServerOptions["onProxyError"]>,
): void {
  const proxyRequest = createUpstreamRequest({
    host: target.host,
    port: target.port,
    method: request.method,
    path: upstreamPath(request.url ?? "/", target),
    headers: cloneProxyHeaders(request.headers, request, target, true),
    agent: false,
  });

  proxyRequest.on("upgrade", (proxyResponse, upstreamSocket, upstreamHead) => {
    writeUpgradeResponse(clientSocket, proxyResponse);
    if (head.length > 0) {
      upstreamSocket.write(head);
    }
    if (upstreamHead.length > 0) {
      clientSocket.write(upstreamHead);
    }
    clientSocket.pipe(upstreamSocket).pipe(clientSocket);
    clientSocket.on("error", () => upstreamSocket.destroy());
    upstreamSocket.on("error", () => clientSocket.destroy());
  });

  proxyRequest.on("response", (proxyResponse) => {
    rejectUpgrade(
      clientSocket,
      proxyResponse.statusCode ?? 502,
      proxyResponse.statusMessage ?? "WebSocket upgrade rejected",
    );
    proxyResponse.resume();
  });

  proxyRequest.on("error", (error) => {
    onProxyError(error, target.id);
    rejectUpgrade(clientSocket, 502, "Hermes WebSocket is unavailable");
  });

  clientSocket.on("close", () => proxyRequest.destroy());
  proxyRequest.end();
}

export function createFleetServer(options: FleetServerOptions = {}): Server {
  const allowedHosts = parseAllowedHosts(options.allowedHosts);
  const upstreams = makeUpstreams(options.upstreamPorts);
  const configuredOperatorLogins = options.operatorLogins;
  const operatorLoginValues: readonly string[] = Array.isArray(configuredOperatorLogins)
    ? configuredOperatorLogins
    : typeof configuredOperatorLogins === "string"
      ? configuredOperatorLogins.split(",")
      : (process.env.HERMES_FLEET_OPERATOR_LOGINS ?? "").split(",");
  const operatorLogins = new Set<string>(
    operatorLoginValues
      .map((login: string) => login.trim().toLowerCase())
      .filter(Boolean),
  );
  const distDir = resolve(
    options.distDir ??
      process.env.HERMES_FLEET_DIST ??
      fileURLToPath(new URL("../dist/", import.meta.url)),
  );
  const snapshotPath = resolve(
    options.snapshotPath ??
      process.env.HERMES_FLEET_SNAPSHOT ??
      "/var/lib/agk-terminal/fleet/fleet-snapshot.json",
  );
  const discordOwnerId = options.discordOwnerId ?? process.env.HERMES_FLEET_DISCORD_OWNER_ID ?? "";
  const routingRequestDir = resolve(options.routingRequestDir ?? process.env.HERMES_FLEET_ROUTING_REQUEST_DIR ?? "/run/user/1000/hermes-fleet-routing");
  const secureStatusDir = resolve(options.secureStatusDir ?? process.env.HERMES_FLEET_SECURE_STATUS_DIR ?? "/var/lib/agk-terminal/fleet/secure-input");
  const discordGuildId = options.discordGuildId ?? process.env.HERMES_FLEET_DISCORD_GUILD_ID ?? "";
  const onProxyError =
    options.onProxyError ??
    ((error: Error, target: OrganisationId) => {
      console.error(`[hermes-fleet] ${target} proxy error: ${error.message}`);
    });

  const server = createServer((request, response) => {
    const authorization = authorizeRequest(request, allowedHosts);
    if (!authorization.allowed) {
      sendText(response, authorization.status, authorization.message);
      return;
    }

    const parsed = new URL(request.url ?? "/", "http://fleet.invalid");
    if (parsed.pathname === "/healthz") {
      const body = JSON.stringify({ status: "ok", organisations: ORGANISATIONS.map(({ id }) => id) });
      response.writeHead(200, {
        "cache-control": "no-store",
        "content-length": Buffer.byteLength(body),
        "content-type": "application/json; charset=utf-8",
        "x-content-type-options": "nosniff",
      });
      response.end(body);
      return;
    }
    if (parsed.pathname === "/api/fleet-snapshot") {
      if (request.method !== "GET") {
        sendText(response, 405, "Method not allowed");
        return;
      }
      void serveFleetSnapshot(
        request,
        response,
        snapshotPath,
        parsed.searchParams.get("org"),
        operatorLogins,
      );
      return;
    }
    if (parsed.pathname === "/api/agent-discord/setup") {
      if (request.method !== "POST") {
        sendText(response, 405, "Method not allowed");
        return;
      }
      const trustedTransport = typeof server.address() === "string" || options.allowMutationOverTcpForTests === true;
      if (!trustedTransport) {
        sendJson(response, 403, { error: "Trusted Unix transport required" });
        return;
      }
      void setupAgentDiscord(request, response, snapshotPath, routingRequestDir, operatorLogins, discordOwnerId);
      return;
    }
    if (parsed.pathname === "/api/agent-discord/secure-input") {
      const trustedTransport = typeof server.address() === "string" || options.allowMutationOverTcpForTests === true;
      if (!trustedTransport) {
        sendJson(response, 403, { error: "Trusted Unix transport required" });
        return;
      }
      if (request.method === "POST") {
        void startAgentSecureInput(request, response, snapshotPath, routingRequestDir, operatorLogins, discordOwnerId, discordGuildId);
        return;
      }
      if (request.method === "GET") {
        void serveSecureInputStatus(request, response, secureStatusDir, operatorLogins, parsed.searchParams.get("id"));
        return;
      }
      sendText(response, 405, "Method not allowed");
      return;
    }

    const target = targetForPath(parsed.pathname, upstreams);
    if (target) {
      proxyHttp(request, response, target, onProxyError);
      return;
    }

    const unprefixedAssetTarget = targetForUnprefixedAsset(
      request,
      parsed.pathname,
      upstreams,
    );
    if (unprefixedAssetTarget) {
      proxyHttp(
        request,
        response,
        unprefixedAssetTarget,
        onProxyError,
        `${parsed.pathname}${parsed.search}`,
      );
      return;
    }

    if (request.method !== "GET" && request.method !== "HEAD") {
      sendText(response, 405, "Method not allowed");
      return;
    }

    void serveStatic(request, response, distDir).catch((error: unknown) => {
      const failure = error instanceof Error ? error : new Error(String(error));
      if (!response.headersSent) {
        sendText(response, 500, "Fleet frontend failed to load");
      } else {
        response.destroy(failure);
      }
    });
  });

  server.on("upgrade", (request, socket, head) => {
    const authorization = authorizeRequest(request, allowedHosts);
    if (!authorization.allowed) {
      rejectUpgrade(socket, authorization.status, authorization.message);
      return;
    }

    const parsed = new URL(request.url ?? "/", "http://fleet.invalid");
    const target = targetForPath(parsed.pathname, upstreams);
    if (!target) {
      rejectUpgrade(socket, 404, "WebSocket route not found");
      return;
    }

    proxyWebSocket(request, socket, head, target, onProxyError);
  });

  server.on("clientError", (_error, socket) => {
    rejectUpgrade(socket, 400, "Bad request");
  });
  server.headersTimeout = 15_000;
  server.keepAliveTimeout = 5_000;

  return server;
}

function configuredPort(value: string | undefined): number {
  const port = value ? Number(value) : DEFAULT_FLEET_PORT;
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new Error("HERMES_FLEET_PORT must be an integer between 1 and 65535");
  }
  return port;
}

export function configuredListenTarget(
  socketValue: string | undefined,
  portValue: string | undefined,
): string | number {
  const socketPath = socketValue?.trim();
  if (socketPath) {
    if (!socketPath.startsWith("/")) {
      throw new Error("HERMES_FLEET_SOCKET must be an absolute path");
    }
    return socketPath;
  }
  return configuredPort(portValue);
}

export function isMainModule(
  entryPath: string | undefined,
  moduleUrl: string,
): boolean {
  if (!entryPath) {
    return false;
  }

  try {
    return realpathSync(entryPath) === realpathSync(fileURLToPath(moduleUrl));
  } catch {
    return false;
  }
}

if (isMainModule(process.argv[1], import.meta.url)) {
  const target = configuredListenTarget(
    process.env.HERMES_FLEET_SOCKET,
    process.env.HERMES_FLEET_PORT,
  );
  const server = createFleetServer();
  const onListening = () => {
    if (typeof target === "string") {
      chmodSync(target, 0o600);
      console.info(`[hermes-fleet] listening on unix:${target}`);
    } else {
      console.info(`[hermes-fleet] listening on http://${FLEET_LISTEN_HOST}:${target}`);
    }
  };
  if (typeof target === "string") {
    try {
      unlinkSync(target);
    } catch (error) {
      if (!(error instanceof Error && "code" in error && error.code === "ENOENT")) {
        throw error;
      }
    }
    server.listen(target, onListening);
  } else {
    server.listen(target, FLEET_LISTEN_HOST, onListening);
  }
}
