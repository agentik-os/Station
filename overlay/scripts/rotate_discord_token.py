#!/usr/bin/env python3
"""Secure, profile-scoped Discord bot token rotation for AGK Station."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple


class Target(NamedTuple):
    user: str
    hermes_home: str
    service: str


TARGETS = {
    "operator": Target("operator", "/home/operator/.hermes", "hermes-gateway.service"),
    "agentik": Target("agentik", "/home/agentik/.hermes", "hermes-gateway.service"),
    "mission": Target("mission", "/home/mission/.hermes", "hermes-gateway.service"),
    "private": Target("private", "/home/private/.hermes", "hermes-gateway.service"),
    "collective": Target("agentik", "/home/agentik/.hermes/profiles/collective", "hermes-gateway-collective.service"),
    "nutrition-os": Target("private", "/home/private/.hermes/profiles/nutrition-os", "hermes-gateway-nutrition-os.service"),
}

_TOKEN = re.compile(r"^[A-Za-z0-9._-]{40,}$")
_TOKEN_KEY = "DISCORD_BOT_TOKEN"


def validate_token_shape(token: str) -> str:
    if token != token.strip() or not _TOKEN.fullmatch(token):
        raise ValueError("Discord token has an invalid shape or contains whitespace")
    return token


def replace_token(content: str, token: str) -> str:
    token = validate_token_shape(token)
    lines = content.splitlines()
    output: list[str] = []
    replaced = False
    for line in lines:
        if line.strip().startswith(f"{_TOKEN_KEY}="):
            if not replaced:
                output.append(f"{_TOKEN_KEY}={token}")
                replaced = True
            continue
        output.append(line)
    if not replaced:
        output.append(f"{_TOKEN_KEY}={token}")
    return "\n".join(output) + "\n"


def discord_identity(token: str) -> dict[str, str]:
    request = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {token}", "User-Agent": "AGK-Station/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as error:
        raise ValueError("Discord rejected the token or identity validation failed") from error
    if not isinstance(raw, dict) or not str(raw.get("id") or "").isdigit() or not raw.get("bot"):
        raise ValueError("Discord token does not identify a bot account")
    return {"id": str(raw["id"]), "username": str(raw.get("username") or "bot")[:80]}


def _systemctl(target: Target, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    account = pwd.getpwnam(target.user)
    runtime = f"/run/user/{account.pw_uid}"
    return subprocess.run(
        [
            "sudo", "-u", target.user, "env",
            f"HOME={account.pw_dir}", f"XDG_RUNTIME_DIR={runtime}",
            f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
            "systemctl", "--user", *arguments,
        ],
        text=True,
        capture_output=True,
        check=check,
    )


def rotate(target_name: str, token: str) -> dict[str, str]:
    if os.geteuid() != 0:
        raise PermissionError("Discord token rotation must run through sudo")
    target = TARGETS[target_name]
    identity = discord_identity(validate_token_shape(token))
    account = pwd.getpwnam(target.user)
    home = Path(target.hermes_home)
    env_path = home / ".env"
    if home.resolve() != home or not home.is_dir() or not env_path.is_file() or env_path.is_symlink():
        raise RuntimeError("Hermes profile or token store is unsafe")
    if env_path.stat().st_uid != account.pw_uid:
        raise RuntimeError("Hermes token store has an unexpected owner")
    original = env_path.read_text(encoding="utf-8")
    updated = replace_token(original, token)
    backup = env_path.with_name(".env.before-discord-token-rotate")
    if backup.exists() or backup.is_symlink():
        backup.unlink()
    shutil.copyfile(env_path, backup)
    os.chown(backup, account.pw_uid, account.pw_gid)
    backup.chmod(0o600)
    temporary_handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=home, prefix=".env.station-new.", delete=False,
    )
    temporary = Path(temporary_handle.name)
    try:
        with temporary_handle:
            temporary_handle.write(updated)
            temporary_handle.flush()
            os.fsync(temporary_handle.fileno())
        os.chown(temporary, account.pw_uid, account.pw_gid)
        temporary.chmod(0o600)
        os.replace(temporary, env_path)
        try:
            _systemctl(target, "restart", target.service)
            state = _systemctl(target, "is-active", target.service).stdout.strip()
            if state != "active":
                raise RuntimeError("gateway did not become active")
        except Exception:
            shutil.copyfile(backup, env_path)
            os.chown(env_path, account.pw_uid, account.pw_gid)
            env_path.chmod(0o600)
            _systemctl(target, "restart", target.service, check=False)
            raise
        backup.unlink(missing_ok=True)
        return {**identity, "target": target_name, "service": target.service, "state": "active"}
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Rotate one Station Discord bot token safely")
    parser.add_argument("action", choices=("rotate", "check", "list"))
    parser.add_argument("target", nargs="?", choices=tuple(TARGETS))
    parser.add_argument("--stdin", action="store_true", help="Read the token from stdin instead of hidden input")
    args = parser.parse_args()
    if args.action == "list":
        for name, target in TARGETS.items():
            print(f"{name:14} {target.user:9} {target.service}")
        return 0
    if not args.target:
        parser.error("target is required for rotate/check")
    target = TARGETS[args.target]
    if args.action == "check":
        env_path = Path(target.hermes_home) / ".env"
        token = ""
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(f"{_TOKEN_KEY}="):
                token = line.split("=", 1)[1].strip().strip('"\'')
                break
        identity = discord_identity(validate_token_shape(token))
        state = _systemctl(target, "is-active", target.service, check=False).stdout.strip()
        print(f"{args.target}: bot={identity['username']} id={identity['id']} service={state or 'unknown'}")
        return 0
    token = sys.stdin.readline().rstrip("\r\n") if args.stdin else getpass.getpass("New Discord bot token: ")
    result = rotate(args.target, token)
    print(f"Rotated {result['target']}: bot={result['username']} id={result['id']} service={result['state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
