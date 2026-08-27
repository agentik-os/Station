#!/usr/bin/env python3
"""Redacted Station health report."""

from __future__ import annotations

import json
import os
import pwd
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple


class Check(NamedTuple):
    name: str
    required: bool
    passed: bool
    detail: str


def exit_code(checks: list[Check]) -> int:
    return 1 if any(check.required and not check.passed for check in checks) else 0


def command_check(name: str) -> Check:
    path = shutil.which(name)
    return Check(f"command:{name}", True, bool(path), path or "missing")


def run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(arguments, text=True, capture_output=True, check=False, timeout=30)


def user_service(user: str, unit: str, *, required: bool = True) -> Check:
    account = pwd.getpwnam(user)
    runtime = f"/run/user/{account.pw_uid}"
    result = run([
        "sudo", "-u", user, "env", f"HOME={account.pw_dir}",
        f"XDG_RUNTIME_DIR={runtime}", f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
        "systemctl", "--user", "is-active", unit,
    ])
    state = result.stdout.strip() or "inactive"
    return Check(f"service:{user}/{unit}", required, result.returncode == 0 and state == "active", state)


def station_board(user: str) -> Check:
    home = f"/home/{user}"
    result = run([
        "sudo", "-u", user, "env", f"HOME={home}", f"HERMES_HOME={home}/.hermes",
        "/opt/agk-terminal/hermes-agent/venv/bin/hermes", "kanban", "boards", "show",
    ])
    expected = f"Current board: {user}-station"
    return Check(f"kanban:{user}", True, result.returncode == 0 and expected in result.stdout, expected if expected in result.stdout else "wrong board")


def portal_check() -> Check:
    request = urllib.request.Request("http://127.0.0.1:8459/healthz", headers={"Host": "localhost"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.load(response)
        passed = payload.get("status") == "ok"
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        passed = False
    return Check("portal:fleet", True, passed, "http://127.0.0.1:8459")


def registry_check() -> Check:
    path = Path("/opt/agentik/os-registry/state/index.json")
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        identities = {f"{row.get('id')}@{row.get('version')}" for row in payload.get("packages", []) if isinstance(row, dict)}
    except (OSError, ValueError, TypeError):
        identities = set()
    expected = {"research-os@0.1.0", "strategy-os@0.1.0", "builder-os@0.1.0", "evaluation-os@0.1.0"}
    return Check("os:core-packages", True, expected.issubset(identities), f"{len(expected & identities)}/{len(expected)}")


def collect_checks() -> list[Check]:
    checks = [command_check(name) for name in ("station", "agk", "hermes", "rmux")]
    for user in ("operator", "agentik", "mission", "private"):
        try:
            pwd.getpwnam(user)
            checks.append(Check(f"user:{user}", True, True, "present"))
        except KeyError:
            checks.append(Check(f"user:{user}", True, False, "missing"))
            continue
        checks.append(user_service(user, "hermes-gateway.service"))
        checks.append(user_service(user, "hermes-serve.service"))
        checks.append(station_board(user))
    checks.extend([
        user_service("operator", "hermes-fleet.service"),
        Check("timer:fleet-snapshot", True, run(["systemctl", "is-active", "agk-fleet-snapshot.timer"]).stdout.strip() == "active", "system timer"),
        portal_check(),
        registry_check(),
    ])
    return checks


def main() -> int:
    checks = collect_checks()
    print("STATION DOCTOR")
    for check in checks:
        icon = "✓" if check.passed else ("✗" if check.required else "!")
        print(f"{icon} {check.name}: {check.detail}")
    required = [check for check in checks if check.required]
    passed = sum(1 for check in required if check.passed)
    print(f"Required checks: {passed}/{len(required)}")
    return exit_code(checks)


if __name__ == "__main__":
    raise SystemExit(main())
