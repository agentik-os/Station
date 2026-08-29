#!/usr/bin/env python3
"""Reconcile stale Collective guild commands after the global panel is live."""
from __future__ import annotations

import argparse
import copy
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://discord.com/api/v10"
APP_ID = "1541131574509314209"
GUILD_ID = "1350170767366688830"
START_CHANNEL = "1541213937096458391"
SIGN_MESSAGE = "1541725051248713808"
REQUIRED_GLOBAL = {"collective", "panel", "clear"}
STALE_GUILD = {"upgrade", "billing", "profile", "opportunities", "learn", "today", "ship", "kudos", "board", "win", "pair", "streak", "deal"}
SIGN_COPY = """Three explicit steps. Everyone signs. No grandfathering for new terms.

**1 · House rules** — read and confirm.
**2 · Deals & referrals** — confirm the 5 / 15 / 80 snapshot and no-bypass rule.
**3 · Sign I ACCEPT** — enter your name and type the exact phrase. Then <@&1541225509940109322> lands.

A ✅ reaction does **not** sign. It privately redirects you to this explicit flow.
We retain your Discord ID, the terms version, UTC timestamps and a one-way hash of the entered name. The legal name itself is not stored in this runtime.

**Signed** opens COLLECTIVE.
**Pro** unlocks LEARN · BUILD · EARN.

Launch: **COLLECTIVE70** = −70% first month. Checkout: paste your Discord ID.
-# partnerships-v2-2026-08-29 · explicit modal consent"""


def load_token() -> str:
    home = Path(os.environ.get("HERMES_HOME") or "/home/mission/.hermes/profiles/collective")
    for raw in (home / ".env").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            if key.strip() in {"DISCORD_BOT_TOKEN", "DISCORD_TOKEN"}:
                return value.strip().strip('"').strip("'")
    raise RuntimeError("Collective Discord credential unavailable")


def request(method: str, path: str, token: str, body=None):
    headers = {"Authorization": "Bot " + token, "User-Agent": "DiscordBot (https://github.com/agentik-os, 1.0)"}
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(8):
        try:
            with urllib.request.urlopen(urllib.request.Request(API + path, data=data, method=method, headers=headers), timeout=30) as response:
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


def canonical_sign_components(components):
    value = copy.deepcopy(components)
    matches = 0
    custom_ids = set()
    def walk(items):
        nonlocal matches
        for item in items or []:
            custom_id = item.get("custom_id")
            if custom_id:
                custom_ids.add(str(custom_id))
            content = str(item.get("content") or "")
            if item.get("type") == 10 and (content == SIGN_COPY or "Three steps" in content or "✅ also signs" in content or "partnerships-v1-2026-08-24" in content):
                item["content"] = SIGN_COPY
                matches += 1
            walk(item.get("components"))
    walk(value)
    if matches != 1:
        raise RuntimeError("Collective sign card copy target is ambiguous")
    if not {"sign_house", "sign_deals", "sign_conduct"}.issubset(custom_ids):
        raise RuntimeError("Collective sign card custom IDs changed unexpectedly")
    return value


def reconcile_sign_card(token: str, apply: bool) -> dict:
    path = f"/channels/{START_CHANNEL}/messages/{SIGN_MESSAGE}"
    before = request("GET", path, token)
    if str((before.get("author") or {}).get("id")) != APP_ID or not (int(before.get("flags") or 0) & 32768):
        raise RuntimeError("Collective sign card identity or V2 flags mismatch")
    desired = canonical_sign_components(before.get("components") or [])
    changed = desired != (before.get("components") or [])
    if apply and changed:
        request("PATCH", path, token, {"components": desired, "flags": 32768})
    reread = request("GET", path, token)
    expected = desired if apply else (before.get("components") or [])
    if apply and reread.get("components") != expected:
        raise RuntimeError("Collective sign card readback mismatch")
    return {"changed": changed, "applied": bool(apply and changed), "message_id": SIGN_MESSAGE}


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
    sign_card = reconcile_sign_card(token, apply)
    return {"apply": apply, "global_required_present": True, "guild_before": sorted(str(item.get("name")) for item in guild_commands), "removed": sorted(removed), "guild_after": remaining, "sign_card": sign_card}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(reconcile(args.apply), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
