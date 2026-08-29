#!/usr/bin/env python3
"""Project deterministic Hermes runtime state into one Discord channel name."""
from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

VALID_STATES = frozenset({"idle", "working", "blocked", "approval"})


class DiscordRateLimited(RuntimeError):
    """A Discord route bucket is unavailable until its exact retry deadline."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = max(0.0, float(retry_after))
        super().__init__(f"Discord rate limited for {self.retry_after:.3f} seconds")


def desired_name(base_name: str, state: str) -> str:
    """Return a bounded channel name while preserving its canonical identity."""
    if state not in VALID_STATES:
        raise ValueError(f"unsupported channel state: {state}")
    base = str(base_name).strip().split("・", 1)[0].strip()
    if not base:
        raise ValueError("empty channel base name")
    return f"{base}・{state}"[:100]


def resolve_state(*, active_agents: int, override: dict | None, now: float) -> str:
    """Resolve a typed, expiring override before the canonical runtime count."""
    if isinstance(override, dict):
        state = override.get("state")
        expires_at = override.get("expires_at")
        if state in VALID_STATES and isinstance(expires_at, (int, float)) and expires_at > now:
            return str(state)
    return "working" if isinstance(active_agents, int) and active_agents > 0 else "idle"


def transition_plan(
    *,
    current: str,
    desired: str,
    pending_state: str | None,
    pending_since: float | None,
    last_applied_at: float,
    now: float,
    debounce_seconds: float,
    cooldown_seconds: float,
) -> dict[str, str | float | None]:
    """Coalesce repeated events and return at most one eligible transition."""
    if desired == current:
        return {"apply": None, "pending_state": None, "pending_since": None}
    if pending_state != desired or pending_since is None:
        return {"apply": None, "pending_state": desired, "pending_since": now}
    if now - pending_since < debounce_seconds:
        return {"apply": None, "pending_state": desired, "pending_since": pending_since}
    if last_applied_at > 0 and now - last_applied_at < cooldown_seconds:
        return {"apply": None, "pending_state": desired, "pending_since": pending_since}
    return {"apply": desired, "pending_state": None, "pending_since": None}


class DiscordClient:
    """Minimal Discord REST client with bounded rate-limit handling and readback."""

    def __init__(
        self,
        token: str,
        *,
        request: Callable[[str, str, dict[str, Any] | None], tuple[int, dict[str, str], dict[str, Any]]] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not token:
            raise ValueError("Discord bot token is required")
        self._token = token
        self._request = request or self._http_request
        self._sleep = sleep

    def _http_request(
        self, method: str, path: str, payload: dict[str, Any] | None
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            "https://discord.com/api/v10" + path,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bot {self._token}",
                "Content-Type": "application/json",
                "User-Agent": "AGK-Station-Channel-State/1.0",
                "X-Audit-Log-Reason": "AGK Station deterministic runtime state",
            },
        )
        try:
            response = urllib.request.urlopen(req, timeout=15)
            raw = response.read()
            return response.status, dict(response.headers.items()), json.loads(raw or b"{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                parsed = json.loads(raw or b"{}")
            except (ValueError, TypeError):
                parsed = {}
            return exc.code, dict(exc.headers.items()), parsed

    def _call(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        status, headers, body = self._request(method, path, payload)
        if 200 <= status < 300:
            return body
        if status == 429:
            raw_delay = headers.get("Retry-After") or body.get("retry_after") or 1
            try:
                retry_after = float(raw_delay)
            except (TypeError, ValueError):
                retry_after = 1.0
            raise DiscordRateLimited(retry_after)
        raise RuntimeError(f"Discord API {method} {path} failed with HTTP {status}")

    def get_channel(self, channel_id: str) -> dict[str, Any]:
        return self._call("GET", f"/channels/{channel_id}", None)

    def rename_and_verify(
        self,
        *,
        channel_id: str,
        desired: str,
        baseline: dict[str, Any],
    ) -> dict[str, Any]:
        self._call("PATCH", f"/channels/{channel_id}", {"name": desired})
        readback = self.get_channel(channel_id)
        expected = {
            "id": str(baseline["id"]),
            "name": desired,
            "parent_id": baseline.get("parent_id"),
            "position": baseline.get("position"),
        }
        actual = {key: readback.get(key) for key in expected}
        actual["id"] = str(actual["id"])
        if actual != expected:
            raise RuntimeError(f"Discord readback invariant failure: expected {expected}, got {actual}")
        return readback


def _read_json(path: Path, *, required: bool = False) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        if required:
            raise RuntimeError("gateway state unavailable") from exc
        return None
    if not isinstance(value, dict):
        if required:
            raise RuntimeError("gateway state unavailable")
        return None
    return value


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def write_override(path: Path, *, state: str, ttl_seconds: float, now: float | None = None) -> None:
    if state not in VALID_STATES:
        raise ValueError(f"unsupported channel state: {state}")
    timestamp = time.time() if now is None else float(now)
    ttl = max(1.0, min(86400.0, float(ttl_seconds)))
    _write_json(Path(path), {"state": state, "expires_at": timestamp + ttl})


def load_dotenv_value(path: Path, key: str) -> str:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"missing required credential: {key}") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            clean = value.strip().strip('"').strip("'")
            if clean:
                return clean
    raise RuntimeError(f"missing required credential: {key}")


def current_state_from_name(base_name: str, name: str) -> str:
    prefix = f"{base_name}・"
    if name.startswith(prefix) and name[len(prefix):] in VALID_STATES:
        return name[len(prefix):]
    return "canonical"


class ChannelStateProjector:
    """Persist and project state changes without polling Discord on every tick."""

    def __init__(
        self,
        *,
        client: Any,
        channel_id: str,
        base_name: str,
        baseline: dict[str, Any],
        gateway_state_path: Path,
        override_path: Path,
        state_path: Path,
        debounce_seconds: float = 3,
        cooldown_seconds: float = 30,
    ) -> None:
        self.client = client
        self.channel_id = str(channel_id)
        self.base_name = base_name
        self.baseline = dict(baseline)
        self.gateway_state_path = Path(gateway_state_path)
        self.override_path = Path(override_path)
        self.state_path = Path(state_path)
        self.debounce_seconds = max(0.0, float(debounce_seconds))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self.current = "canonical"
        self.pending_state: str | None = None
        self.pending_since: float | None = None
        self.last_applied_at = 0.0
        self.rate_limited_until = 0.0

    def initialize(self, *, now: float | None = None) -> dict[str, Any]:
        channel = self.client.get_channel(self.channel_id)
        expected_structure = {
            "id": str(self.baseline["id"]),
            "parent_id": self.baseline.get("parent_id"),
            "position": self.baseline.get("position"),
        }
        actual_structure = {
            "id": str(channel.get("id")),
            "parent_id": channel.get("parent_id"),
            "position": channel.get("position"),
        }
        if actual_structure != expected_structure:
            raise RuntimeError(
                f"Discord readback invariant failure: expected {expected_structure}, got {actual_structure}"
            )
        self.current = current_state_from_name(self.base_name, str(channel.get("name") or ""))
        persisted = _read_json(self.state_path) or {}
        self.pending_state = persisted.get("pending_state") if persisted.get("pending_state") in VALID_STATES else None
        raw_since = persisted.get("pending_since")
        self.pending_since = float(raw_since) if isinstance(raw_since, (int, float)) else None
        raw_applied = persisted.get("last_applied_at")
        self.last_applied_at = float(raw_applied) if isinstance(raw_applied, (int, float)) else 0.0
        raw_rate_limit = persisted.get("rate_limited_until")
        self.rate_limited_until = float(raw_rate_limit) if isinstance(raw_rate_limit, (int, float)) else 0.0
        self._save(now=now if now is not None else time.time())
        return channel

    def _save(self, *, now: float) -> None:
        _write_json(
            self.state_path,
            {
                "channel_id": self.channel_id,
                "base_name": self.base_name,
                "current": self.current,
                "pending_state": self.pending_state,
                "pending_since": self.pending_since,
                "last_applied_at": self.last_applied_at,
                "rate_limited_until": self.rate_limited_until,
                "updated_at": now,
            },
        )

    def tick(self, *, now: float | None = None) -> str | None:
        timestamp = time.time() if now is None else float(now)
        gateway = _read_json(self.gateway_state_path, required=True)
        assert gateway is not None
        active_agents = gateway.get("active_agents")
        if not isinstance(active_agents, int) or active_agents < 0:
            raise RuntimeError("gateway state unavailable")
        desired = resolve_state(
            active_agents=active_agents,
            override=_read_json(self.override_path),
            now=timestamp,
        )
        if timestamp < self.rate_limited_until:
            if desired != self.current and self.pending_state != desired:
                self.pending_state = desired
                self.pending_since = timestamp
            elif desired == self.current:
                self.pending_state = None
                self.pending_since = None
            self._save(now=timestamp)
            return None
        plan = transition_plan(
            current=self.current,
            desired=desired,
            pending_state=self.pending_state,
            pending_since=self.pending_since,
            last_applied_at=self.last_applied_at,
            now=timestamp,
            debounce_seconds=self.debounce_seconds,
            cooldown_seconds=self.cooldown_seconds,
        )
        self.pending_state = plan["pending_state"] if isinstance(plan["pending_state"], str) else None
        raw_pending_since = plan["pending_since"]
        self.pending_since = float(raw_pending_since) if isinstance(raw_pending_since, (int, float)) else None
        apply_state = plan["apply"]
        if isinstance(apply_state, str):
            name = desired_name(self.base_name, apply_state)
            try:
                self.client.rename_and_verify(
                    channel_id=self.channel_id,
                    desired=name,
                    baseline=self.baseline,
                )
            except DiscordRateLimited as exc:
                self.pending_state = apply_state
                self.pending_since = timestamp
                self.rate_limited_until = timestamp + exc.retry_after
                self._save(now=timestamp)
                return None
            self.current = apply_state
            self.last_applied_at = timestamp
            self.rate_limited_until = 0.0
            self._save(now=timestamp)
            return name
        self._save(now=timestamp)
        return None


def load_targets(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(Path(path), required=True)
    assert payload is not None
    rows = payload.get("targets")
    if not isinstance(rows, list) or len(rows) != 5:
        raise ValueError("channel-state manifest must contain exactly five targets")
    required = {"key", "user", "hermes_home", "channel_id", "base_name", "parent_id", "position"}
    result: list[dict[str, Any]] = []
    channel_ids: set[str] = set()
    keys: set[str] = set()
    for raw in rows:
        if not isinstance(raw, dict) or not required.issubset(raw):
            raise ValueError("invalid channel-state target")
        row = dict(raw)
        key = str(row["key"])
        user = str(row["user"])
        base_name = str(row["base_name"])
        row["channel_id"] = str(row["channel_id"])
        row["parent_id"] = str(row["parent_id"])
        row["position"] = int(row["position"])
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", key):
            raise ValueError("invalid channel-state key")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user):
            raise ValueError("invalid channel-state user")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", base_name):
            raise ValueError("invalid channel base name")
        if not re.fullmatch(r"[0-9]{17,20}", row["channel_id"]):
            raise ValueError("invalid Discord channel ID")
        if not re.fullmatch(r"[0-9]{17,20}", row["parent_id"]):
            raise ValueError("invalid Discord parent ID")
        if row["position"] < 0:
            raise ValueError("invalid channel position")
        try:
            account_home = Path(pwd.getpwnam(user).pw_dir).resolve(strict=True)
        except (KeyError, OSError) as exc:
            raise ValueError("invalid channel-state user") from exc
        hermes_home = Path(str(row["hermes_home"])).resolve(strict=False)
        canonical_home = account_home / ".hermes"
        profile_parent = canonical_home / "profiles"
        is_profile = hermes_home.parent == profile_parent and bool(
            re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", hermes_home.name)
        )
        if hermes_home != canonical_home and not is_profile:
            raise ValueError("profile boundary violation")
        row["key"] = key
        row["user"] = user
        row["base_name"] = base_name
        row["hermes_home"] = str(hermes_home)
        if row["key"] in keys or row["channel_id"] in channel_ids:
            raise ValueError("duplicate channel-state target")
        desired_name(str(row["base_name"]), "idle")
        keys.add(str(row["key"]))
        channel_ids.add(row["channel_id"])
        result.append(row)
    return result


def render_unit(target: dict[str, Any], *, script_path: str) -> str:
    values = {key: str(target[key]) for key in (
        "key", "user", "hermes_home", "channel_id", "base_name", "parent_id", "position"
    )}
    if any("\n" in value or " " in value for value in values.values()):
        raise ValueError("unsafe systemd target value")
    return f"""[Unit]
Description=AGK Station Discord channel state projector ({values['key']})
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=HOME=/home/{values['user']}
Environment=HERMES_HOME={values['hermes_home']}
ExecStart=/usr/bin/python3 {script_path} --hermes-home {values['hermes_home']} run --channel-id {values['channel_id']} --base-name {values['base_name']} --parent-id {values['parent_id']} --position {values['position']}
Restart=on-failure
RestartSec=15
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=default.target
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_target(command: argparse.ArgumentParser) -> None:
        command.add_argument("--channel-id", required=True)
        command.add_argument("--base-name", required=True)
        command.add_argument("--parent-id", required=True)
        command.add_argument("--position", required=True, type=int)

    run = subparsers.add_parser("run")
    add_target(run)
    run.add_argument("--poll-seconds", type=float, default=2.0)
    run.add_argument("--debounce-seconds", type=float, default=3.0)
    run.add_argument("--cooldown-seconds", type=float, default=30.0)
    run.add_argument("--once", action="store_true")

    restore = subparsers.add_parser("restore")
    add_target(restore)

    state = subparsers.add_parser("set-state")
    state.add_argument("state", choices=sorted(VALID_STATES))
    state.add_argument("--ttl", type=float, default=300.0)
    subparsers.add_parser("clear-state")
    return parser


def _paths(home: Path) -> tuple[Path, Path, Path]:
    return (
        home / "gateway_state.json",
        home / "discord_channel_state_override.json",
        home / "discord_channel_state.json",
    )


def _client(home: Path) -> DiscordClient:
    token = load_dotenv_value(home / ".env", "DISCORD_BOT_TOKEN")
    return DiscordClient(token)


def _baseline(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "id": str(args.channel_id),
        "parent_id": str(args.parent_id),
        "position": int(args.position),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    home = Path(args.hermes_home).resolve()
    gateway_path, override_path, state_path = _paths(home)

    if args.command == "set-state":
        write_override(override_path, state=args.state, ttl_seconds=args.ttl)
        print(json.dumps({"status": "override-set", "state": args.state, "ttl_seconds": args.ttl}))
        return 0
    if args.command == "clear-state":
        override_path.unlink(missing_ok=True)
        print(json.dumps({"status": "override-cleared"}))
        return 0

    client = _client(home)
    baseline = _baseline(args)
    if args.command == "restore":
        readback = client.rename_and_verify(
            channel_id=str(args.channel_id),
            desired=str(args.base_name),
            baseline=baseline,
        )
        override_path.unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)
        print(json.dumps({
            "status": "restored",
            "channel_id": str(readback.get("id")),
            "name": str(readback.get("name")),
        }))
        return 0

    projector = ChannelStateProjector(
        client=client,
        channel_id=str(args.channel_id),
        base_name=str(args.base_name),
        baseline=baseline,
        gateway_state_path=gateway_path,
        override_path=override_path,
        state_path=state_path,
        debounce_seconds=args.debounce_seconds,
        cooldown_seconds=args.cooldown_seconds,
    )
    initial = projector.initialize()
    print(json.dumps({
        "status": "started",
        "channel_id": str(initial.get("id")),
        "name": str(initial.get("name")),
    }), flush=True)
    while True:
        changed = projector.tick()
        if changed:
            print(json.dumps({
                "status": "projected",
                "channel_id": str(args.channel_id),
                "name": changed,
            }), flush=True)
        if args.once:
            return 0
        time.sleep(max(1.0, min(60.0, float(args.poll_seconds))))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:  # noqa: BLE001 - top-level service boundary.
        print(json.dumps({"status": "error", "error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(1)
