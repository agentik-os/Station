#!/usr/bin/env python3
"""Reconcile stale Collective guild commands after the global panel is live."""
from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://discord.com/api/v10"
APP_ID = "1541131574509314209"
GUILD_ID = "1350170767366688830"
REQUIRED_GLOBAL = {"collective", "panel", "clear"}
STALE_GUILD = {"upgrade", "billing", "profile", "opportunities", "learn", "today", "ship", "kudos", "board", "win", "pair", "streak", "deal"}


def load_token() -> str:
    home = Path(os.environ.get("HERMES_HOME") or "/home/mission/.hermes/profiles/collective")
    for raw in (home / ".env").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if key.strip() in {"DISCORD_BOT_TOKEN", "DISCORD_TOKEN"}:
                return value.strip().strip('"').strip("'")
    raise RuntimeError("Collective Discord credential unavailable")


def request(method: str, path: str, token: str):
    headers = {"Authorization": "Bot " + token, "User-Agent": "DiscordBot (https://github.com/agentik-os, 1.0)"}
    for attempt in range(8):
        try:
            with urllib.request.urlopen(urllib.request.Request(API + path, method=method, headers=headers), timeout=30) as response:
                raw = response.read()
                return json.loads(raw.decode() or "{}") if raw else {}
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            if error.code == 429 and attempt < 7:
                try:
                    delay = float(json.loads(raw).get("retry_after", 1))
                except Exception:
                    delay = 1
                time.sleep(min(max(delay, 0.1), 60))
                continue
            raise RuntimeError(f"Discord {method} {path} failed HTTP {error.code}") from error
    raise RuntimeError("Discord command reconciliation retry budget exhausted")


def reconcile(apply: bool) -> dict:
    token = load_token()
    global_commands = request("GET", f"/applications/{APP_ID}/commands", token)
    guild_commands = request("GET", f"/applications/{APP_ID}/guilds/{GUILD_ID}/commands", token)
    global_names = {str(item.get("name")) for item in global_commands}
    missing = sorted(REQUIRED_GLOBAL - global_names)
    if missing:
        raise RuntimeError("Required global command surfaces are not live: " + ",".join(missing))
    unknown = sorted(str(item.get("name")) for item in guild_commands if str(item.get("name")) not in STALE_GUILD)
    if unknown:
        raise RuntimeError("Unknown guild commands require owner review: " + ",".join(unknown))
    removed = []
    if apply:
        for item in guild_commands:
            name = str(item.get("name"))
            command_id = str(item.get("id") or "")
            if name in STALE_GUILD and command_id:
                request("DELETE", f"/applications/{APP_ID}/guilds/{GUILD_ID}/commands/{command_id}", token)
                removed.append(name)
    reread = request("GET", f"/applications/{APP_ID}/guilds/{GUILD_ID}/commands", token)
    remaining = sorted(str(item.get("name")) for item in reread)
    if apply and remaining:
        raise RuntimeError("Guild command reconciliation readback is not empty")
    return {"apply": apply, "global_required_present": True, "guild_before": sorted(str(item.get("name")) for item in guild_commands), "removed": sorted(removed), "guild_after": remaining}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(reconcile(args.apply), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
