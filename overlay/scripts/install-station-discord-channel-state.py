#!/usr/bin/env python3
"""Install, audit, or rollback AGK Discord channel-state projectors."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pwd
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECTOR_SOURCE = HERE / "station_discord_channel_state.py"
MANIFEST_SOURCE = HERE.parent / "config" / "discord-channel-state.json"
INSTALL_ROOT = Path("/usr/local/lib/agk-terminal/scripts")
PROJECTOR_INSTALLED = INSTALL_ROOT / "station_discord_channel_state.py"

SPEC = importlib.util.spec_from_file_location("station_discord_channel_state", PROJECTOR_SOURCE)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def copy_file(source: Path, destination: Path) -> bool:
    """Copy a file unless source and destination are the same installed path."""
    source = Path(source)
    destination = Path(destination)
    if source.resolve(strict=True) == destination.resolve(strict=False):
        return False
    shutil.copy2(source, destination)
    return True


def user_systemctl(user: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    account = pwd.getpwnam(user)
    env = [
        f"HOME={account.pw_dir}",
        f"XDG_RUNTIME_DIR=/run/user/{account.pw_uid}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{account.pw_uid}/bus",
        "PATH=/usr/local/bin:/usr/bin:/bin",
    ]
    return subprocess.run(
        [
            "/usr/bin/setpriv",
            f"--reuid={account.pw_uid}",
            f"--regid={account.pw_gid}",
            "--clear-groups",
            "/usr/bin/env",
            *env,
            "/usr/bin/systemctl",
            "--user",
            *args,
        ],
        text=True,
        capture_output=True,
        timeout=60,
        check=check,
    )


def unit_path(target: dict) -> Path:
    return Path(f"/home/{target['user']}/.config/systemd/user/station-discord-channel-state-{target['key']}.service")


def install(manifest: Path) -> dict:
    if os.geteuid() != 0:
        raise PermissionError("root is required")
    targets = MODULE.load_targets(manifest)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = Path("/var/lib/station/backups/discord-channel-state") / timestamp
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    installed_units: list[str] = []
    prior_units: dict[str, dict] = {}

    INSTALL_ROOT.mkdir(mode=0o755, parents=True, exist_ok=True)
    projector_existed = PROJECTOR_INSTALLED.exists()
    if projector_existed:
        shutil.copy2(PROJECTOR_INSTALLED, backup / PROJECTOR_INSTALLED.name)
    copy_file(PROJECTOR_SOURCE, PROJECTOR_INSTALLED)
    PROJECTOR_INSTALLED.chmod(0o755)

    users: set[str] = set()
    for target in targets:
        account = pwd.getpwnam(target["user"])
        path = unit_path(target)
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        prior_units[str(path)] = {
            "existed": path.exists(),
            "enabled": user_systemctl(target["user"], "is-enabled", path.name, check=False).returncode == 0,
            "active": user_systemctl(target["user"], "is-active", path.name, check=False).returncode == 0,
            "user": target["user"],
        }
        if path.exists():
            shutil.copy2(path, backup / path.name)
        temporary = path.with_name(f".{path.name}.new")
        temporary.write_text(
            MODULE.render_unit(target, script_path=str(PROJECTOR_INSTALLED)),
            encoding="utf-8",
        )
        os.chown(temporary, account.pw_uid, account.pw_gid)
        temporary.chmod(0o644)
        temporary.replace(path)
        installed_units.append(str(path))
        users.add(target["user"])

    try:
        for user in sorted(users):
            user_systemctl(user, "daemon-reload")
        for target in targets:
            name = unit_path(target).name
            user_systemctl(target["user"], "enable", name)
            user_systemctl(target["user"], "restart", name)
    except Exception:
        for target in reversed(targets):
            path = unit_path(target)
            prior = prior_units[str(path)]
            user_systemctl(target["user"], "disable", "--now", path.name, check=False)
            saved = backup / path.name
            if prior["existed"] and saved.exists():
                shutil.copy2(saved, path)
            else:
                path.unlink(missing_ok=True)
        if projector_existed and (backup / PROJECTOR_INSTALLED.name).exists():
            shutil.copy2(backup / PROJECTOR_INSTALLED.name, PROJECTOR_INSTALLED)
        elif PROJECTOR_SOURCE.resolve(strict=True) != PROJECTOR_INSTALLED.resolve(strict=False):
            PROJECTOR_INSTALLED.unlink(missing_ok=True)
        for user in sorted(users):
            user_systemctl(user, "daemon-reload", check=False)
        for path_text, prior in prior_units.items():
            path = Path(path_text)
            if prior["enabled"]:
                user_systemctl(prior["user"], "enable", path.name, check=False)
            if prior["active"]:
                user_systemctl(prior["user"], "start", path.name, check=False)
        raise

    return {
        "status": "installed",
        "backup": str(backup),
        "script": str(PROJECTOR_INSTALLED),
        "units": installed_units,
    }


def audit(manifest: Path) -> dict:
    targets = MODULE.load_targets(manifest)
    rows = []
    for target in targets:
        name = unit_path(target).name
        result = user_systemctl(
            target["user"],
            "show",
            name,
            "-p", "ActiveState",
            "-p", "SubState",
            "-p", "MainPID",
            "-p", "NRestarts",
            check=False,
        )
        values = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        rows.append({
            "key": target["key"],
            "user": target["user"],
            "channel_id": target["channel_id"],
            "unit": name,
            **values,
        })
    return {"status": "audited", "targets": rows}


def rollback(manifest: Path) -> dict:
    if os.geteuid() != 0:
        raise PermissionError("root is required")
    targets = MODULE.load_targets(manifest)
    users: set[str] = set()
    restored = []
    errors = []
    for target in targets:
        name = unit_path(target).name
        user_systemctl(target["user"], "disable", "--now", name, check=False)
        account = pwd.getpwnam(target["user"])
        command = [
            "/usr/bin/setpriv", f"--reuid={account.pw_uid}", f"--regid={account.pw_gid}",
            "--clear-groups", "/usr/bin/env", f"HOME={account.pw_dir}",
            f"HERMES_HOME={target['hermes_home']}", "/usr/bin/python3", str(PROJECTOR_INSTALLED),
            "--hermes-home", target["hermes_home"], "restore",
            "--channel-id", target["channel_id"], "--base-name", target["base_name"],
            "--parent-id", target["parent_id"], "--position", str(target["position"]),
        ]
        restored_target = False
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=60, check=True)
            restored.append(json.loads(result.stdout))
            restored_target = True
        except (subprocess.SubprocessError, ValueError) as exc:
            errors.append({"key": target["key"], "error": type(exc).__name__})
        if restored_target:
            unit_path(target).unlink(missing_ok=True)
        else:
            # Preserve a working, retryable projector when Discord defers rollback.
            user_systemctl(target["user"], "enable", "--now", name, check=False)
        users.add(target["user"])
    for user in sorted(users):
        user_systemctl(user, "daemon-reload")
    if not errors:
        PROJECTOR_INSTALLED.unlink(missing_ok=True)
    return {
        "status": "rolled-back" if not errors else "rollback-incomplete",
        "restored": restored,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "audit", "rollback"))
    parser.add_argument("--manifest", type=Path, default=MANIFEST_SOURCE)
    args = parser.parse_args()
    if args.action == "install":
        result = install(args.manifest)
    elif args.action == "rollback":
        result = rollback(args.manifest)
    else:
        result = audit(args.manifest)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result.get("status") == "rollback-incomplete" else 0


if __name__ == "__main__":
    raise SystemExit(main())
