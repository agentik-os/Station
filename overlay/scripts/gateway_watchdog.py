#!/usr/bin/env python3
"""Alert once when an AGK profile bot stays unavailable for ten minutes.

Routine gateway stop/start messages are disabled in Hermes configuration.
This watchdog is deliberately out-of-process, so it can still notify Discord
when the profile gateway itself is down.  Recovery is silent.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml


DEFAULT_STATE = Path("/var/lib/agk-terminal/gateway-watchdog.json")
DEFAULT_HOME_ROOT = Path("/home")
DEFAULT_NOTIFIER_HOME = Path("/home/operator/.hermes")
DEFAULT_THRESHOLD = 600
DEFAULT_OWNER_ID = "1441423462492016821"
DISCORD_API = "https://discord.com/api/v10"


@dataclass(frozen=True)
class ProfileBot:
    name: str
    hermes_home: Path
    required_platforms: tuple[str, ...]


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a YAML object")
    return data


def discover_profile_bots(home_root: Path = DEFAULT_HOME_ROOT) -> list[ProfileBot]:
    candidates = list(home_root.glob("*/.hermes/config.yaml"))
    candidates.extend(home_root.glob("*/.hermes/profiles/*/config.yaml"))
    profiles: list[ProfileBot] = []
    for config_path in sorted(candidates):
        try:
            config = _read_yaml(config_path)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        platforms = config.get("platforms") or {}
        if not isinstance(platforms, dict):
            continue
        required = tuple(
            name
            for name in ("discord", "telegram")
            if isinstance(platforms.get(name), dict)
            and bool(platforms[name].get("enabled"))
        )
        if not required:
            continue
        hermes_home = config_path.parent.resolve()
        # A named Hermes profile may intentionally inherit the parent bot
        # configuration while never owning a gateway process of its own.  Do
        # not report that shadow profile as a separate outage. A gateway
        # becomes monitorable after its first start writes gateway_state.json,
        # or when provisioning explicitly marks it as expected.
        discord_extra = (
            platforms.get("discord", {}).get("extra", {})
            if isinstance(platforms.get("discord"), dict)
            else {}
        )
        explicitly_expected = bool(
            isinstance(discord_extra, dict)
            and discord_extra.get("offline_alert_enabled") is True
        )
        if not (hermes_home / "gateway_state.json").exists() and not explicitly_expected:
            continue
        relative = hermes_home.relative_to(home_root.resolve())
        parts = relative.parts
        linux_user = parts[0]
        profile_name = parts[3] if len(parts) >= 4 and parts[2] == "profiles" else None
        name = f"{linux_user}/{profile_name}" if profile_name else linux_user
        profiles.append(ProfileBot(name, hermes_home, required))
    return profiles


def _pid_is_gateway(pid: Any) -> bool:
    try:
        numeric = int(pid)
    except (TypeError, ValueError):
        return False
    if numeric <= 1:
        return False
    try:
        command = Path(f"/proc/{numeric}/cmdline").read_bytes().replace(b"\0", b" ")
    except OSError:
        return False
    return b"gateway" in command and b"run" in command and b"hermes" in command


def profile_health(profile: ProfileBot) -> tuple[bool, str]:
    state_path = profile.hermes_home / "gateway_state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "gateway state unavailable"
    if state.get("gateway_state") != "running" or not _pid_is_gateway(state.get("pid")):
        return False, "gateway process unavailable"
    platforms = state.get("platforms") or {}
    for platform in profile.required_platforms:
        platform_state = platforms.get(platform) or {}
        if platform_state.get("state") != "connected" or platform_state.get("error_code"):
            return False, f"{platform} disconnected"
    return True, "connected"


def gateway_unit(profile: ProfileBot) -> tuple[str, str]:
    linux_user, separator, profile_name = profile.name.partition("/")
    unit = "hermes-gateway.service" if not separator else f"hermes-gateway-{profile_name}.service"
    return linux_user, unit


def attempt_recovery(
    profile: ProfileBot,
    reason: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
    account_lookup: Callable[[str], Any] = pwd.getpwnam,
) -> bool:
    """Start an enabled missing gateway without overriding maintenance or masks."""

    if reason not in {"gateway state unavailable", "gateway process unavailable"}:
        return False
    if (profile.hermes_home / ".drain_request.json").exists():
        return False
    try:
        state = json.loads((profile.hermes_home / "gateway_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if state.get("gateway_state") == "draining":
        return False

    linux_user, unit = gateway_unit(profile)
    account = account_lookup(linux_user)
    runtime = f"/run/user/{account.pw_uid}"
    prefix = [
        "/usr/sbin/runuser", "-u", linux_user, "--", "env",
        f"HOME={account.pw_dir}", f"XDG_RUNTIME_DIR={runtime}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
        "systemctl", "--user",
    ]
    enabled = runner(
        [*prefix, "is-enabled", unit],
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if enabled.returncode != 0 or str(enabled.stdout).strip() != "enabled":
        return False
    started = runner(
        [*prefix, "start", unit],
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    return started.returncode == 0


def _env_value(path: Path, key: str) -> str | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        current, value = stripped.split("=", 1)
        if current.strip() != key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        return value or None
    return None


def _discord_json(
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{DISCORD_API}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "AGK-Gateway-Watchdog/1",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read()
    return json.loads(raw) if raw else None


def notify_owner_dm(notifier_home: Path, owner_id: str, message: str) -> bool:
    token = _env_value(notifier_home / ".env", "DISCORD_BOT_TOKEN")
    if not token or not str(owner_id).isdigit():
        return False
    try:
        direct = _discord_json(
            token,
            "POST",
            "/users/@me/channels",
            {"recipient_id": str(owner_id)},
        ) or {}
        channel_id = str(direct.get("id") or "")
        if not channel_id.isdigit():
            return False
        result = _discord_json(
            token,
            "POST",
            f"/channels/{channel_id}/messages",
            {"content": message, "allowed_mentions": {"parse": []}},
        )
        return isinstance(result, dict) and bool(result.get("id"))
    except (OSError, ValueError, yaml.YAMLError, urllib.error.URLError):
        return False


def evaluate_profile(
    record: dict[str, Any] | None,
    *,
    healthy: bool,
    reason: str,
    now: float,
    threshold: int,
    send: Callable[[], bool],
) -> dict[str, Any] | None:
    """Advance one outage state. Healthy recovery intentionally returns None."""

    if healthy:
        return None
    current = dict(record or {})
    current.setdefault("down_since", now)
    current["reason"] = reason
    current.setdefault("alerted", False)
    if not current["alerted"] and now - float(current["down_since"]) >= threshold:
        if send():
            current["alerted"] = True
            current["alerted_at"] = now
    return current


def _load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def run_once(
    *,
    state_path: Path = DEFAULT_STATE,
    home_root: Path = DEFAULT_HOME_ROOT,
    notifier_home: Path = DEFAULT_NOTIFIER_HOME,
    threshold: int = DEFAULT_THRESHOLD,
    now: float | None = None,
) -> dict[str, Any]:
    timestamp = time.time() if now is None else float(now)
    previous = _load_state(state_path)
    next_state: dict[str, Any] = {}
    for profile in discover_profile_bots(home_root):
        healthy, reason = profile_health(profile)
        recovered = False if healthy else attempt_recovery(profile, reason)

        def send(profile: ProfileBot = profile, reason: str = reason) -> bool:
            minutes = max(1, threshold // 60)
            return notify_owner_dm(
                notifier_home,
                DEFAULT_OWNER_ID,
                f"🔴 AGK · `{profile.name}` est hors ligne depuis {minutes} minutes ({reason}).",
            )

        record = evaluate_profile(
            previous.get(str(profile.hermes_home)),
            healthy=healthy,
            reason=reason,
            now=timestamp,
            threshold=threshold,
            send=send,
        )
        if record is not None:
            if recovered:
                record["recovery_attempted_at"] = timestamp
            next_state[str(profile.hermes_home)] = record
    _save_state(state_path, next_state)
    return next_state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--home-root", type=Path, default=DEFAULT_HOME_ROOT)
    parser.add_argument("--notifier-home", type=Path, default=DEFAULT_NOTIFIER_HOME)
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    args = parser.parse_args()
    run_once(
        state_path=args.state,
        home_root=args.home_root,
        notifier_home=args.notifier_home,
        threshold=max(60, args.threshold),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
