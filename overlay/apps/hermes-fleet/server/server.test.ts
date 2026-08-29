import { mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import {
  createServer,
  request as createRequest,
  type IncomingHttpHeaders,
  type Server,
} from "node:http";
import type { AddressInfo } from "node:net";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { pathToFileURL } from "node:url";

import { describe, expect, it } from "vitest";

import {
  configuredListenTarget,
  createFleetServer,
  isMainModule,
  parseAllowedHosts,
} from "./server.js";

interface HttpResult {
  body: string;
  headers: IncomingHttpHeaders;
  status: number;
}

async function listen(server: Server): Promise<number> {
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.off("error", reject);
      resolve();
    });
  });
  return (server.address() as AddressInfo).port;
}

async function close(server: Server): Promise<void> {
  if (!server.listening) {
    return;
  }
  await new Promise<void>((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
}

async function httpGet(
  port: number,
  path: string,
  headers: Record<string, string> = {},
): Promise<HttpResult> {
  return await new Promise<HttpResult>((resolve, reject) => {
    const request = createRequest(
      {
        host: "127.0.0.1",
        port,
        method: "GET",
        path,
        headers,
      },
      (response) => {
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => chunks.push(chunk));
        response.on("end", () => {
          resolve({
            body: Buffer.concat(chunks).toString("utf8"),
            headers: response.headers,
            status: response.statusCode ?? 0,
          });
        });
      },
    );
    request.on("error", reject);
    request.end();
  });
}

async function httpJson(
  port: number,
  path: string,
  body: unknown,
  headers: Record<string, string> = {},
): Promise<HttpResult> {
  return await new Promise<HttpResult>((resolve, reject) => {
    const payload = JSON.stringify(body);
    const request = createRequest({
      host: "127.0.0.1", port, method: "POST", path,
      headers: { ...headers, "content-type": "application/json", "content-length": Buffer.byteLength(payload) },
    }, (response) => {
      const chunks: Buffer[] = [];
      response.on("data", (chunk: Buffer) => chunks.push(chunk));
      response.on("end", () => resolve({ body: Buffer.concat(chunks).toString("utf8"), headers: response.headers, status: response.statusCode ?? 0 }));
    });
    request.on("error", reject);
    request.end(payload);
  });
}

describe("Fleet request boundaries", () => {
  it("prefers an absolute permissioned Unix socket over loopback TCP", () => {
    expect(configuredListenTarget(" /run/user/1000/hermes-fleet.sock ", "8459"))
      .toBe("/run/user/1000/hermes-fleet.sock");
    expect(configuredListenTarget(undefined, "8459")).toBe(8459);
    expect(() => configuredListenTarget("relative.sock", "8459")).toThrow(
      "HERMES_FLEET_SOCKET must be an absolute path",
    );
  });

  it("recognizes the compiled entrypoint through an immutable release symlink", async () => {
    const temporary = await mkdtemp(join(tmpdir(), "hermes-fleet-entry-test-"));
    const releaseEntry = join(temporary, "release-server.js");
    const linkedEntry = join(temporary, "server.js");
    await writeFile(releaseEntry, "export {};\n");
    await symlink(releaseEntry, linkedEntry);

    try {
      expect(isMainModule(linkedEntry, pathToFileURL(releaseEntry).href)).toBe(true);
    } finally {
      await rm(temporary, { recursive: true });
    }
  });

  it("parses configured hosts while always retaining loopback", () => {
    expect(
      parseAllowedHosts("fleet.example.ts.net, dashboard.example.ts.net"),
    ).toEqual(
      new Set([
        "localhost",
        "127.0.0.1",
        "fleet.example.ts.net",
        "dashboard.example.ts.net",
      ]),
    );
  });

  it("serves health and static assets only for allowed hosts", async () => {
    const temporary = await mkdtemp(join(tmpdir(), "hermes-fleet-test-"));
    await writeFile(join(temporary, "index.html"), "<h1>Fleet ready</h1>");
    const fleet = createFleetServer({
      allowedHosts: ["fleet.test"],
      distDir: temporary,
    });
    const port = await listen(fleet);

    try {
      const health = await httpGet(port, "/healthz", { host: "fleet.test" });
      expect(health.status).toBe(200);
      expect(JSON.parse(health.body)).toMatchObject({ status: "ok" });

      const staticPage = await httpGet(port, "/", { host: "fleet.test" });
      expect(staticPage.status).toBe(200);
      expect(staticPage.body).toContain("Fleet ready");

      const rejected = await httpGet(port, "/healthz", {
        host: "untrusted.test",
      });
      expect(rejected.status).toBe(421);
      expect(rejected.body).toContain("Host is not allowed");
    } finally {
      await close(fleet);
      await rm(temporary, { recursive: true });
    }
  });

  it("serves a global Operator snapshot and strictly scoped station snapshots", async () => {
    const temporary = await mkdtemp(join(tmpdir(), "hermes-fleet-snapshot-test-"));
    const snapshotPath = join(temporary, "fleet-snapshot.json");
    await writeFile(snapshotPath, JSON.stringify({
      schema: "agk.fleet.v1",
      generated_at: 100,
      organisations: {
        operator: { id: "operator", kanban: { tasks: [{ title: "Global ops" }] } },
        private: { id: "private", kanban: { tasks: [{ title: "Private goal" }] } },
      },
    }));
    const fleet = createFleetServer({
      allowedHosts: ["fleet.test"],
      operatorLogins: ["owner@example.com"],
      distDir: temporary,
      snapshotPath,
    });
    const port = await listen(fleet);

    try {
      const operator = await httpGet(port, "/api/fleet-snapshot?org=operator", {
        host: "fleet.test",
        "tailscale-user-login": "owner@example.com",
      });
      expect(operator.status).toBe(200);
      expect(Object.keys(JSON.parse(operator.body).organisations)).toEqual([
        "operator", "private",
      ]);

      const privateResult = await httpGet(port, "/api/fleet-snapshot?org=private", {
        host: "fleet.test",
        "tailscale-user-login": "owner@example.com",
      });
      expect(privateResult.status).toBe(200);
      expect(Object.keys(JSON.parse(privateResult.body).organisations)).toEqual([
        "private",
      ]);
      expect(privateResult.body).not.toContain("Global ops");
      expect(privateResult.body).not.toContain("Private goal");

      const privateDenied = await httpGet(port, "/api/fleet-snapshot?org=private", {
        host: "fleet.test",
      });
      expect(privateDenied.status).toBe(403);

      const unknown = await httpGet(port, "/api/fleet-snapshot?org=unknown", {
        host: "fleet.test",
      });
      expect(unknown.status).toBe(400);
    } finally {
      await close(fleet);
      await rm(temporary, { recursive: true });
    }
  });

  it("denies the global Operator snapshot without the owner identity", async () => {
    const temporary = await mkdtemp(join(tmpdir(), "hermes-fleet-owner-test-"));
    const snapshotPath = join(temporary, "fleet-snapshot.json");
    await writeFile(snapshotPath, JSON.stringify({
      schema: "agk.fleet.v1",
      generated_at: 100,
      organisations: { operator: { id: "operator" } },
    }));
    const fleet = createFleetServer({
      allowedHosts: ["fleet.test"],
      operatorLogins: ["owner@example.com"],
      distDir: temporary,
      snapshotPath,
    });
    const port = await listen(fleet);

    try {
      const denied = await httpGet(port, "/api/fleet-snapshot?org=operator", {
        host: "fleet.test",
      });
      expect(denied.status).toBe(403);

      const wrongOwner = await httpGet(port, "/api/fleet-snapshot?org=operator", {
        host: "fleet.test",
        "tailscale-user-login": "other@example.com",
      });
      expect(wrongOwner.status).toBe(403);
    } finally {
      await close(fleet);
      await rm(temporary, { recursive: true });
    }
  });

  it("rejects spoofed owner headers over direct TCP mutation transport", async () => {
    const fleet = createFleetServer({ allowedHosts: ["fleet.test"], operatorLogins: ["owner@example.com"], discordOwnerId: "1441423462492016821" });
    const port = await listen(fleet);
    try {
      const result = await httpJson(port, "/api/agent-discord/setup", { organisation: "private", profile: "nutrition-os", application_id: "1542135948475637861", channel_id: "1542137541572956193" }, { host: "fleet.test", "tailscale-user-login": "owner@example.com" });
      expect(result.status).toBe(403);
      expect(result.body).toContain("Trusted Unix transport required");
    } finally { await close(fleet); }
  });

  it("locks per-agent Discord routing to Gareth and the exact dedicated channel", async () => {
    const temporary = await mkdtemp(join(tmpdir(), "hermes-fleet-discord-setup-"));
    const snapshotPath = join(temporary, "fleet-snapshot.json");
    const requestDir = join(temporary, "requests");
    const secureStatusDir = join(temporary, "secure-status");
    await writeFile(snapshotPath, JSON.stringify({ schema: "agk.fleet.v1", generated_at: 1, organisations: { private: { agents: [{ id: "nutrition-os", profile: "nutrition-os" }], os: [{ id: "nutrition-os", version: "1.1.0", installed: true }] } } }));
    const fleet = createFleetServer({ allowedHosts: ["fleet.test"], operatorLogins: ["owner@example.com"], discordOwnerId: "1441423462492016821", discordGuildId: "1541131439599386644", routingRequestDir: requestDir, secureStatusDir, allowMutationOverTcpForTests: true, distDir: temporary, snapshotPath });
    const port = await listen(fleet);
    try {
      const denied = await httpJson(port, "/api/agent-discord/setup", { organisation: "private", profile: "nutrition-os", application_id: "1542135948475637861", channel_id: "1542137541572956193" }, { host: "fleet.test" });
      expect(denied.status).toBe(403);
      const invalid = await httpJson(port, "/api/agent-discord/setup", { organisation: "private", profile: "nutrition-os", channel_id: "abc" }, { host: "fleet.test", "tailscale-user-login": "owner@example.com" });
      expect(invalid.status).toBe(400);
      const configured = await httpJson(port, "/api/agent-discord/setup", { organisation: "private", profile: "nutrition-os", application_id: "1542135948475637861", channel_id: "1542137541572956193", allowed_user: "someone-else" }, { host: "fleet.test", "tailscale-user-login": "owner@example.com" });
      expect(configured.status).toBe(200);
      const configuredPayload = JSON.parse(configured.body) as { request_id: string };
      expect(configuredPayload).toMatchObject({ queued: true, owner_id: "1441423462492016821", channel_id: "1542137541572956193", restart_required: true });
      const files = await readdir(requestDir);
      expect(files).toHaveLength(1);
      expect(JSON.parse(await readFile(join(requestDir, files[0]!), "utf8"))).toEqual({ schema: "agk.agent-discord-routing.v1", organisation: "private", profile: "nutrition-os", application_id: "1542135948475637861", channel_id: "1542137541572956193", owner_id: "1441423462492016821" });

      const secure = await httpJson(port, "/api/agent-discord/secure-input", { organisation: "private", profile: "nutrition-os", application_id: "1542135948475637861", channel_id: "1542137541572956193" }, { host: "fleet.test", "tailscale-user-login": "owner@example.com" });
      expect(secure.status).toBe(200);
      const securePayload = JSON.parse(secure.body) as { request_id: string };
      expect(securePayload.request_id).toMatch(/^\d+-\d+-[0-9a-f]+$/);
      const queued = JSON.parse(await readFile(join(requestDir, `${securePayload.request_id}.json`), "utf8"));
      expect(queued).toMatchObject({ schema: "agk.agent-discord-secure-input.v1", application_id: "1542135948475637861", guild_id: "1541131439599386644", owner_id: "1441423462492016821" });
      const pending = await httpGet(port, `/api/agent-discord/secure-input?id=${securePayload.request_id}`, { host: "fleet.test", "tailscale-user-login": "owner@example.com" });
      expect(pending.status).toBe(202);
      await mkdir(secureStatusDir, { recursive: true });
      await writeFile(join(secureStatusDir, `${securePayload.request_id}.jsonl`), JSON.stringify({ status: "READY", url: "https://agk-core.test.ts/secure-route", expires_in_seconds: 1800, transport: "tailscale-serve-https" }) + "\n");
      const ready = await httpGet(port, `/api/agent-discord/secure-input?id=${securePayload.request_id}`, { host: "fleet.test", "tailscale-user-login": "owner@example.com" });
      expect(ready.status).toBe(200);
      expect(JSON.parse(ready.body)).toMatchObject({ status: "ready", url: "https://agk-core.test.ts/secure-route" });
    } finally {
      await close(fleet); await rm(temporary, { recursive: true });
    }
  });

  it("rejects a foreign browser Origin even when Host is allowed", async () => {
    const fleet = createFleetServer({ allowedHosts: ["fleet.test"] });
    const port = await listen(fleet);

    try {
      const rejected = await httpGet(port, "/healthz", {
        host: "fleet.test",
        origin: "https://untrusted.test",
      });
      expect(rejected.status).toBe(403);
      expect(rejected.body).toContain("Origin is not allowed");

      const mismatchedAllowedOrigin = await httpGet(port, "/healthz", {
        host: "fleet.test",
        origin: "http://localhost",
      });
      expect(mismatchedAllowedOrigin.status).toBe(403);

      const tailscaleProxyOrigin = await httpGet(port, "/healthz", {
        host: "localhost",
        origin: "https://fleet.test",
        "tailscale-user-login": "owner@example.com",
      });
      expect(tailscaleProxyOrigin.status).toBe(200);
    } finally {
      await close(fleet);
    }
  });
});

describe("Hermes HTTP proxy", () => {
  it("strips the organization path and rewrites protected headers", async () => {
    let observedUrl = "";
    let observedHeaders: IncomingHttpHeaders = {};
    const upstream = createServer((request, response) => {
      observedUrl = request.url ?? "";
      observedHeaders = request.headers;
      response.statusCode = 201;
      response.setHeader("location", "/login?next=/sessions");
      response.setHeader("set-cookie", "hermes_session=test; Path=/; HttpOnly");
      response.end("proxied");
    });
    const upstreamPort = await listen(upstream);
    const fleet = createFleetServer({
      allowedHosts: ["fleet.test"],
      upstreamPorts: { operator: upstreamPort },
    });
    const fleetPort = await listen(fleet);

    try {
      const result = await httpGet(fleetPort, "/operator/api/session?view=all", {
        host: "fleet.test",
        origin: "https://fleet.test",
      });

      expect(result.status).toBe(201);
      expect(result.body).toBe("proxied");
      expect(observedUrl).toBe("/api/session?view=all");
      expect(observedHeaders.host).toBe(`127.0.0.1:${upstreamPort}`);
      expect(observedHeaders.origin).toBe(`http://127.0.0.1:${upstreamPort}`);
      expect(observedHeaders["x-forwarded-prefix"]).toBe("/operator");
      expect(observedHeaders["x-forwarded-host"]).toBe("fleet.test");
      expect(observedHeaders["x-forwarded-proto"]).toBe("https");
      expect(result.headers.location).toBe("/operator/login?next=/sessions");
      expect(result.headers["set-cookie"]).toEqual([
        "hermes_session=test; Path=/operator/; HttpOnly",
      ]);
    } finally {
      await close(fleet);
      await close(upstream);
    }
  });

  it("routes lazy root chunks from a same-origin dashboard referrer", async () => {
    let observedUrl = "";
    const upstream = createServer((request, response) => {
      observedUrl = request.url ?? "";
      response.end("hermes-lazy-chunk");
    });
    const upstreamPort = await listen(upstream);
    const temporary = await mkdtemp(join(tmpdir(), "hermes-fleet-assets-test-"));
    await mkdir(join(temporary, "assets"));
    await writeFile(join(temporary, "assets", "fleet.js"), "fleet-shell");
    const fleet = createFleetServer({
      allowedHosts: ["fleet.test"],
      distDir: temporary,
      upstreamPorts: { operator: upstreamPort },
    });
    const fleetPort = await listen(fleet);

    try {
      const lazyChunk = await httpGet(
        fleetPort,
        "/assets/SessionsPage.js?revision=1",
        {
          host: "fleet.test",
          referer: "https://fleet.test/operator/sessions",
        },
      );
      expect(lazyChunk.status).toBe(200);
      expect(lazyChunk.body).toBe("hermes-lazy-chunk");
      expect(observedUrl).toBe("/assets/SessionsPage.js?revision=1");

      const fleetAsset = await httpGet(fleetPort, "/assets/fleet.js", {
        host: "fleet.test",
        referer: "https://fleet.test/?org=operator",
      });
      expect(fleetAsset.status).toBe(200);
      expect(fleetAsset.body).toBe("fleet-shell");

      observedUrl = "";
      const foreignReferrer = await httpGet(
        fleetPort,
        "/assets/SessionsPage.js",
        {
          host: "fleet.test",
          referer: "https://untrusted.test/operator/sessions",
        },
      );
      expect(foreignReferrer.status).toBe(404);
      expect(observedUrl).toBe("");
    } finally {
      await close(fleet);
      await close(upstream);
      await rm(temporary, { recursive: true });
    }
  });
});

describe("Hermes WebSocket proxy", () => {
  it("forwards an upgrade through the same path and header boundary", async () => {
    let observedUrl = "";
    let observedHeaders: IncomingHttpHeaders = {};
    const upstream = createServer();
    upstream.on("upgrade", (request, socket) => {
      observedUrl = request.url ?? "";
      observedHeaders = request.headers;
      socket.end(
        "HTTP/1.1 101 Switching Protocols\r\n" +
          "Connection: Upgrade\r\n" +
          "Upgrade: websocket\r\n\r\n" +
          "fleet-ws-ready",
      );
    });
    const upstreamPort = await listen(upstream);
    const fleet = createFleetServer({
      allowedHosts: ["fleet.test"],
      upstreamPorts: { mission: upstreamPort },
    });
    const fleetPort = await listen(fleet);

    try {
      const payload = await new Promise<string>((resolve, reject) => {
        const request = createRequest({
          host: "127.0.0.1",
          port: fleetPort,
          path: "/mission/ws/events?scope=active",
          headers: {
            connection: "Upgrade",
            host: "fleet.test",
            origin: "https://fleet.test",
            upgrade: "websocket",
          },
        });
        request.on("upgrade", (_response, socket, head) => {
          const chunks: Uint8Array[] = [head];
          socket.on("data", (chunk: Buffer) => chunks.push(chunk));
          socket.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
          socket.on("error", reject);
        });
        request.on("response", (response) => {
          reject(new Error(`Expected upgrade, received ${response.statusCode}`));
        });
        request.on("error", reject);
        request.end();
      });

      expect(payload).toContain("fleet-ws-ready");
      expect(observedUrl).toBe("/ws/events?scope=active");
      expect(observedHeaders.host).toBe(`127.0.0.1:${upstreamPort}`);
      expect(observedHeaders.origin).toBe(`http://127.0.0.1:${upstreamPort}`);
      expect(observedHeaders["x-forwarded-prefix"]).toBe("/mission");
    } finally {
      await close(fleet);
      await close(upstream);
    }
  });
});
