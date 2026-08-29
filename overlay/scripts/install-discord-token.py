#!/usr/bin/env python3
"""Validate a Discord bot token from stdin and store it in one isolated Hermes vault."""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


class CredentialActivationUncertain(RuntimeError):
    """The submitted credential may still be loaded; never hide it via disk rollback."""


@dataclass(frozen=True)
class FileSnapshot:
    content: bytes
    mode: int
    uid: int
    gid: int


_BOT_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SNOWFLAKE = re.compile(r"^[0-9]{15,22}$")


def canonical_bot_id(value: str) -> str:
    bot_id = str(value or "")
    if not _BOT_ID.fullmatch(bot_id):
        raise ValueError("bot id must use canonical lowercase kebab grammar")
    return bot_id


def invite_url(application_id: str) -> str:
    return (
        "https://discord.com/oauth2/authorize?client_id="
        + str(application_id)
        + "&scope=bot%20applications.commands&permissions=274877975552"
    )


def _discord_json(path: str, token: str):
    request = urllib.request.Request(
        "https://discord.com/api/v10" + path,
        headers={"Authorization": "Bot " + token, "Accept": "application/json", "User-Agent": "AGK-Station/1"},
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            return json.loads(response.read())
    except Exception as exc:
        raise ValueError("Discord rejected the credential") from exc


def validate_discord_token(secret: str) -> dict:
    identity = _discord_json("/users/@me", secret)
    guild_rows = _discord_json("/users/@me/guilds", secret)
    application = _discord_json("/oauth2/applications/@me", secret)
    if not isinstance(identity, dict) or not identity.get("id"):
        raise ValueError("Discord identity validation failed")
    if not isinstance(application, dict) or not application.get("id"):
        raise ValueError("Discord application validation failed")
    application_bot = application.get("bot")
    if not isinstance(application_bot, dict) or str(application_bot.get("id") or "") != str(identity["id"]):
        raise ValueError("Discord application identity does not match the bot")
    if not isinstance(guild_rows, list):
        raise ValueError("Discord guild validation failed")
    guilds = [str(row.get("id")) for row in guild_rows if isinstance(row, dict) and row.get("id")]
    return {
        "id": str(identity["id"]),
        "username": str(identity.get("username") or "bot")[:100],
        "application_id": str(application["id"]),
        "guilds": guilds,
    }


def _inside(path: Path, root: Path) -> bool:
    target, allowed = path.resolve(), root.resolve()
    return target == allowed or allowed in target.parents


def _validate_owned_profile_tree(profile_root: Path, profiles_root: Path) -> tuple[Path, Path]:
    raw_profile = Path(profile_root).expanduser().absolute()
    raw_profiles = Path(profiles_root).expanduser().absolute()
    uid, gid = os.geteuid(), os.getegid()
    if raw_profile.parent != raw_profiles:
        raise ValueError("profile target escapes the canonical profiles root")
    for path in (raw_profiles, *raw_profiles.parents, raw_profile):
        if path != Path("/") and os.path.lexists(path) and path.is_symlink():
            raise ValueError("canonical profile tree contains a symlinked ancestor")
    owned_chain = [raw_profiles.parents[1], raw_profiles.parent, raw_profiles, raw_profile]
    for path in owned_chain:
        if not os.path.lexists(path) or path.is_symlink() or not path.is_dir():
            raise ValueError("unsafe canonical profile tree")
        st = path.stat()
        if st.st_uid != uid or st.st_gid != gid:
            raise ValueError("canonical profile tree has the wrong owner")
    return raw_profile.resolve(), raw_profiles.resolve()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_owned_systemd_tree(service_root: Path) -> Path:
    root = Path(service_root).expanduser().absolute()
    if len(root.parents) < 3:
        raise ValueError("unsafe systemd user configuration root")
    home = root.parents[2]
    uid, gid = os.geteuid(), os.getegid()
    for path in (root, *root.parents):
        if path != Path("/") and os.path.lexists(path) and path.is_symlink():
            raise ValueError("systemd tree contains an unsafe ancestor")
    chain = [home, home / ".config", home / ".config/systemd", root]
    for path in chain:
        if os.path.lexists(path):
            if path.is_symlink() or not path.is_dir():
                raise ValueError("systemd tree contains an unsafe ancestor")
        else:
            path.mkdir(mode=0o700)
        st = path.stat()
        if st.st_uid != uid or st.st_gid != gid:
            raise ValueError("systemd tree has the wrong owner")
    return root.resolve()


def _validate_private_distribution(profile_root: Path, expected_os_id: str, expected_version: str) -> None:
    distribution = Path(profile_root) / "distribution.yaml"
    if distribution.is_symlink() or not distribution.is_file():
        raise ValueError("Private OS distribution is unavailable")
    try:
        manifest = yaml.safe_load(distribution.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("Private OS distribution is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("owner_environment") != "private"
        or manifest.get("profile_id") != expected_os_id
        or manifest.get("os_id") != expected_os_id
        or str(manifest.get("version") or "") != str(expected_version)
    ):
        raise ValueError("Private OS distribution identity mismatch")


def finalization_commands(
    profile_id: str,
    home_channel: str,
    *,
    hermes_bin: str = "/usr/local/bin/hermes",
) -> list[list[str]]:
    profile = canonical_bot_id(profile_id)
    channel = str(home_channel or "")
    if not _SNOWFLAKE.fullmatch(channel):
        raise ValueError("invalid Discord home channel")
    prefix = [str(hermes_bin), "--profile", profile]
    return [
        [*prefix, "config", "set", "platforms.discord.enabled", "true"],
        [*prefix, "config", "set", "platforms.discord.gateway_restart_notification", "false"],
        [*prefix, "config", "set", "discord.require_mention", "true"],
        [*prefix, "config", "set", "discord.allowed_channels", channel],
        [*prefix, "config", "set", "discord.free_response_channels", channel],
        [*prefix, "config", "set", "agent.restart_after_turn_timeout", "1800"],
        [*prefix, "config", "set", "agent.restart_drain_timeout", "1800"],
        [*prefix, "gateway", "install", "--force", "--start-now", "--start-on-login"],
        [*prefix, "doctor"],
    ]


def _read_gateway_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_gateway_state_strict(path: Path) -> dict:
    if not path.exists():
        return {"_state_known": True}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"_state_known": False}
    if not isinstance(value, dict):
        return {"_state_known": False}
    return {**value, "_state_known": True}


def _service_state(unit: str) -> dict:
    result = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState", "-p", "FragmentPath", "--no-pager"],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    active_state = values.get("ActiveState")
    load_state = values.get("LoadState")
    unit_file_state = values.get("UnitFileState") or ""
    stable_unit_states = {
        "enabled", "enabled-runtime", "disabled", "static", "indirect",
        "masked", "masked-runtime", "linked", "linked-runtime", "generated",
    }
    unit_state_known = (
        unit_file_state in {"", "not-found"}
        if load_state == "not-found"
        else unit_file_state in stable_unit_states
    )
    known = (
        result.returncode == 0
        and load_state in {"loaded", "not-found"}
        and active_state in {"active", "inactive", "failed"}
        and unit_state_known
    )
    return {
        "known": known,
        "load_state": load_state,
        "active_state": active_state,
        "unit_file_state": unit_file_state,
        "active": values.get("ActiveState") == "active",
        "enabled": values.get("UnitFileState") == "enabled",
        "unit_path": values.get("FragmentPath") or None,
    }


def _gateway_argv_matches(argv: list[str], profile_id: str) -> bool:
    return argv == [
        "/opt/agk-terminal/hermes-agent/venv/bin/python",
        "-m", "hermes_cli.main", "--profile", canonical_bot_id(profile_id),
        "gateway", "run",
    ]


def _gateway_process_alive(pid: int, profile_id: str) -> bool:
    try:
        argv = [
            value.decode("utf-8", "strict")
            for value in Path(f"/proc/{int(pid)}/cmdline").read_bytes().split(b"\0")
            if value
        ]
    except (OSError, ValueError, UnicodeError):
        return False
    return _gateway_argv_matches(argv, profile_id)


def _parse_proc_start_time(raw: str) -> int:
    remainder = raw.rsplit(") ", 1)[1].split()
    return int(remainder[19])


def _gateway_process_start_time(pid: int) -> int | None:
    try:
        return _parse_proc_start_time(
            Path(f"/proc/{int(pid)}/stat").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, IndexError):
        return None


def preflight_profile(
    profile_root: Path,
    *,
    profile_id: str,
    service_state_reader: Callable[[str], dict] = _service_state,
    state_reader: Callable[[Path], dict] = _read_gateway_state_strict,
    process_alive: Callable[[int, str], bool] = _gateway_process_alive,
    canonical_profiles_root: Path | None = None,
    service_config_root: Path | None = None,
) -> None:
    root, profiles_root = _validate_owned_profile_tree(
        Path(profile_root), Path(canonical_profiles_root or (Path.home() / ".hermes/profiles"))
    )
    if root.name != canonical_bot_id(profile_id):
        raise ValueError("unsafe Hermes profile target")
    unit = f"hermes-gateway-{profile_id}.service"
    service_root = _validate_owned_systemd_tree(
        Path(service_config_root or (Path.home() / ".config/systemd/user"))
    )
    if (service_root / unit).exists() or (service_root / f"{unit}.d").exists():
        raise ValueError("active gateway requires the separate rotation workflow")
    service = service_state_reader(unit)
    if not service.get("known"):
        raise RuntimeError("gateway service state is unavailable")
    state = state_reader(root / "gateway_state.json")
    if state.get("_state_known") is False:
        raise RuntimeError("gateway state is unavailable")
    prior_pid = int(state.get("pid") or 0)
    if (
        service.get("load_state") != "not-found"
        or service.get("active_state") != "inactive"
        or service.get("unit_file_state") not in {"", "not-found"}
        or service.get("active")
        or service.get("enabled")
        or service.get("unit_path")
        or (prior_pid and process_alive(prior_pid, profile_id))
    ):
        raise ValueError("active gateway requires the separate rotation workflow")


def ensure_profile_quiescent(
    profile_root: Path,
    *,
    profile_id: str,
    service_state_reader: Callable[[str], dict] = _service_state,
    state_reader: Callable[[Path], dict] = _read_gateway_state_strict,
    process_alive: Callable[[int, str], bool] = _gateway_process_alive,
) -> None:
    unit = f"hermes-gateway-{canonical_bot_id(profile_id)}.service"
    service = service_state_reader(unit)
    state = state_reader(Path(profile_root).resolve() / "gateway_state.json")
    if state.get("_state_known") is False:
        raise RuntimeError("submitted credential may still be active")
    pid = int(state.get("pid") or 0)
    if (
        not service.get("known")
        or service.get("load_state") != "not-found"
        or service.get("active_state") != "inactive"
        or service.get("unit_file_state") not in {"", "not-found"}
        or service.get("active")
        or service.get("enabled")
        or service.get("unit_path")
        or (pid and process_alive(pid, profile_id))
    ):
        raise RuntimeError("submitted credential may still be active")


def _atomic_json(path: Path, payload: dict) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise


def _write_runtime_drop_in(root: Path, profile_id: str) -> Path:
    base = Path(root)
    if base.is_symlink():
        raise ValueError("unsafe systemd user configuration root")
    directory = base / f"hermes-gateway-{canonical_bot_id(profile_id)}.service.d"
    if directory.is_symlink():
        raise ValueError("unsafe gateway service drop-in target")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / "30-station-runtime.conf"
    if path.is_symlink():
        raise ValueError("unsafe gateway service drop-in target")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("[Service]\nTimeoutStopSec=1860\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    return path


def _snapshot_file(path: Path) -> FileSnapshot | None:
    if not os.path.lexists(path):
        return None
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"unsafe snapshot target: {path.name}")
    st = path.stat()
    return FileSnapshot(path.read_bytes(), stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid)


def _file_matches(path: Path, snapshot: FileSnapshot | None) -> bool:
    if snapshot is None:
        return not os.path.lexists(path)
    if path.is_symlink() or not path.is_file():
        return False
    st = path.stat()
    return (
        path.read_bytes() == snapshot.content
        and stat.S_IMODE(st.st_mode) == snapshot.mode
        and st.st_uid == snapshot.uid
        and st.st_gid == snapshot.gid
    )


def _restore_file(path: Path, previous: FileSnapshot | None) -> None:
    if previous is None:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        if os.path.lexists(path):
            raise RuntimeError(f"rollback unlink readback failed: {path.name}")
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.rollback-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, previous.mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(previous.content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        _fsync_directory(path.parent)
        os.chmod(path, previous.mode)
        if path.stat().st_uid != previous.uid or path.stat().st_gid != previous.gid:
            os.chown(path, previous.uid, previous.gid)
    finally:
        temporary.unlink(missing_ok=True)
    if not _file_matches(path, previous):
        raise RuntimeError(f"rollback readback failed: {path.name}")


def finalize_profile(
    profile_root: Path,
    *,
    profile_id: str,
    home_channel: str,
    application_id: str,
    guild_id: str,
    hermes_bin: str = "/usr/local/bin/hermes",
    runner: Callable = subprocess.run,
    state_reader: Callable[[Path], dict] = _read_gateway_state_strict,
    wait: Callable[[float], None] = time.sleep,
    attempts: int = 30,
    service_config_root: Path | None = None,
    service_state_reader: Callable[[str], dict] = _service_state,
    process_alive: Callable[[int, str], bool] = _gateway_process_alive,
    process_start_time: Callable[[int], int | None] = _gateway_process_start_time,
    state_mtime: Callable[[Path], float] = lambda path: path.stat().st_mtime,
    wall_clock: Callable[[], float] = time.time,
    canonical_profiles_root: Path | None = None,
) -> dict:
    root, profiles_root = _validate_owned_profile_tree(
        Path(profile_root), Path(canonical_profiles_root or (Path.home() / ".hermes/profiles"))
    )
    if root.name != canonical_bot_id(profile_id):
        raise ValueError("unsafe Hermes profile target")
    if not _SNOWFLAKE.fullmatch(str(application_id)) or not _SNOWFLAKE.fullmatch(str(guild_id)):
        raise ValueError("invalid Discord identity")

    unit = f"hermes-gateway-{profile_id}.service"
    service_root = _validate_owned_systemd_tree(
        Path(service_config_root or (Path.home() / ".config/systemd/user"))
    )
    if (service_root / unit).exists() or (service_root / f"{unit}.d").exists():
        raise ValueError("active gateway requires the separate rotation workflow")
    service_before = service_state_reader(unit)
    if not service_before.get("known"):
        raise RuntimeError("gateway service state is unavailable")
    if (
        service_before.get("load_state") != "not-found"
        or service_before.get("active_state") != "inactive"
        or service_before.get("unit_file_state") not in {"", "not-found"}
        or service_before.get("active")
        or service_before.get("enabled")
        or service_before.get("unit_path")
    ):
        raise ValueError("active gateway requires the separate rotation workflow")
    state_path = root / "gateway_state.json"
    prior_state = state_reader(state_path)
    if prior_state.get("_state_known") is False:
        raise RuntimeError("gateway state is unavailable")
    prior_pid = int(prior_state.get("pid") or 0)
    if prior_pid and process_alive(prior_pid, profile_id):
        raise ValueError("active gateway requires the separate rotation workflow")
    started_at = float(wall_clock())
    dropin = service_root / f"{unit}.d/30-station-runtime.conf"
    dropin_dir_before = os.path.lexists(dropin.parent)
    unit_path = service_root / unit
    config_path = root / "config.yaml"
    receipt_path = root / "discord-install-receipt.json"
    config_before = _snapshot_file(config_path)
    dropin_before = _snapshot_file(dropin)
    receipt_before = _snapshot_file(receipt_path)
    state_before = _snapshot_file(state_path)
    unit_paths = {unit_path}
    fragment = service_before.get("unit_path")
    if fragment:
        unit_paths.add(Path(str(fragment)))
    unit_before = {
        path: _snapshot_file(path)
        for path in unit_paths
    }
    launched_pid = 0
    launched_start_time: int | None = None
    gateway_start_attempted = False

    try:
        state_path.unlink(missing_ok=True)
        _fsync_directory(state_path.parent)
        if os.path.lexists(state_path):
            raise RuntimeError("gateway state nonce removal failed")
        _write_runtime_drop_in(service_root, profile_id)
        for command in finalization_commands(profile_id, home_channel, hermes_bin=hermes_bin):
            if "gateway" in command and "install" in command:
                gateway_start_attempted = True
            timeout = 180 if "gateway" in command and "install" in command else (120 if command[-1] == "doctor" else 30)
            result = runner(command, text=True, capture_output=True, timeout=timeout)
            if result.returncode:
                raise RuntimeError("Hermes Discord finalization command failed")

        connected = False
        state: dict = {}
        for _ in range(max(1, int(attempts))):
            state = state_reader(state_path)
            pid = state.get("pid")
            start_time = state.get("start_time")
            if (
                pid
                and int(pid) != prior_pid
                and isinstance(start_time, int)
                and start_time > 0
            ):
                launched_pid = int(pid)
                launched_start_time = start_time
            discord = (state.get("platforms") or {}).get("discord") or {}
            try:
                fresh_file = state_mtime(state_path) >= started_at
            except OSError:
                fresh_file = False
            if (
                state.get("gateway_state") == "running"
                and pid
                and isinstance(start_time, int)
                and start_time > 0
                and int(pid) != prior_pid
                and process_alive(int(pid), profile_id)
                and process_start_time(int(pid)) == start_time
                and fresh_file
                and discord.get("state") == "connected"
                and discord.get("writer_pid") == pid
                and discord.get("writer_start_time") == start_time
            ):
                connected = True
                break
            wait(1.0)
        if not connected:
            raise RuntimeError("Hermes Discord gateway did not connect freshly")

        service_after = service_state_reader(unit)
        expected_fragment = str((service_root / unit).resolve())
        if (
            not service_after.get("known")
            or service_after.get("load_state") != "loaded"
            or service_after.get("active_state") != "active"
            or service_after.get("unit_file_state") != "enabled"
            or not service_after.get("active")
            or not service_after.get("enabled")
            or str(service_after.get("unit_path") or "") != expected_fragment
        ):
            raise RuntimeError("gateway systemd final readback failed")

        receipt = {
            "schema": "agk.os-discord-install.v1",
            "profile_id": str(profile_id),
            "application_id": str(application_id),
            "guild_id": str(guild_id),
            "home_channel": str(home_channel),
            "gateway": "connected",
            "pid": int(state["pid"]),
            "start_time": state["start_time"],
        }
        _atomic_json(receipt_path, receipt)
        if json.loads(receipt_path.read_text(encoding="utf-8")) != receipt or (receipt_path.stat().st_mode & 0o777) != 0o600:
            raise RuntimeError("Discord install receipt readback failed")
        _fsync_file(state_path)
        _fsync_directory(state_path.parent)
        final_state = state_reader(state_path)
        final_discord = (final_state.get("platforms") or {}).get("discord") or {}
        if (
            final_state.get("_state_known") is False
            or final_state.get("pid") != state.get("pid")
            or final_state.get("start_time") != state.get("start_time")
            or final_discord.get("state") != "connected"
            or final_discord.get("writer_pid") != state.get("pid")
            or final_discord.get("writer_start_time") != state.get("start_time")
        ):
            raise RuntimeError("gateway state durable readback failed")
        return receipt
    except Exception as exc:
        rollback_errors = []
        try:
            stopped = runner(["systemctl", "--user", "disable", "--now", unit], text=True, capture_output=True, timeout=180)
            if stopped.returncode:
                rollback_errors.append("disable --now failed")
        except Exception:
            rollback_errors.append("disable --now raised")
        if gateway_start_attempted and not launched_pid:
            try:
                observed = state_reader(state_path)
            except Exception:
                observed = {"_state_known": False}
            observed_pid = int(observed.get("pid") or 0)
            observed_start = observed.get("start_time")
            if observed_pid != prior_pid and isinstance(observed_start, int) and observed_start > 0:
                launched_pid = observed_pid
                launched_start_time = observed_start
        new_process_dead = True
        if gateway_start_attempted and not (launched_pid and launched_start_time):
            new_process_dead = False
            rollback_errors.append("new gateway process identity is unavailable")
        elif launched_pid and launched_start_time:
            new_process_dead = False
            for _ in range(30):
                if process_start_time(launched_pid) != launched_start_time:
                    new_process_dead = True
                    break
                wait(0.5)
            if not new_process_dead:
                rollback_errors.append("new gateway process is still alive")
        restore_targets = [
            (config_path, config_before),
            (dropin, dropin_before),
            (receipt_path, receipt_before),
            *unit_before.items(),
        ]
        if new_process_dead:
            restore_targets.append((state_path, state_before))
        for path, previous in restore_targets:
            try:
                _restore_file(path, previous)
            except Exception:
                rollback_errors.append(f"restore failed: {path.name}")
        if not dropin_dir_before:
            try:
                dropin.parent.rmdir()
                _fsync_directory(dropin.parent.parent)
                if os.path.lexists(dropin.parent):
                    rollback_errors.append("drop-in directory readback failed")
            except OSError:
                rollback_errors.append("drop-in directory cleanup failed")

        try:
            reloaded = runner(["systemctl", "--user", "daemon-reload"], text=True, capture_output=True, timeout=30)
            if reloaded.returncode:
                rollback_errors.append("daemon-reload failed")
        except Exception:
            rollback_errors.append("daemon-reload raised")
        if service_before.get("enabled"):
            try:
                enabled = runner(["systemctl", "--user", "enable", unit], text=True, capture_output=True, timeout=30)
                if enabled.returncode:
                    rollback_errors.append("restore enable failed")
            except Exception:
                rollback_errors.append("restore enable raised")
        try:
            after = service_state_reader(unit)
        except Exception:
            after = {}
            rollback_errors.append("service state readback raised")
        expected_enabled = bool(service_before.get("enabled"))
        if (
            not after.get("known")
            or after.get("active_state") != service_before.get("active_state")
            or after.get("load_state") != service_before.get("load_state")
            or after.get("unit_file_state") != service_before.get("unit_file_state")
            or bool(after.get("enabled")) != expected_enabled
        ):
            rollback_errors.append("service state did not restore")
        if rollback_errors:
            if not new_process_dead:
                raise CredentialActivationUncertain(
                    "submitted credential activation cannot be disproved; gateway state was not restored"
                ) from exc
            raise RuntimeError("Hermes Discord rollback failed: " + "; ".join(rollback_errors)) from exc
        raise RuntimeError("Hermes Discord finalization failed and was rolled back") from exc


def _replace_env_values(current: list[str], values: dict[str, str]) -> list[str]:
    output = []
    for line in current:
        candidate = line.strip()
        if candidate.startswith("export "):
            candidate = candidate[7:].lstrip()
        key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        if key not in values:
            output.append(line)
    output.extend(key + "=" + value for key, value in values.items())
    return output


def install_token(
    secret: str,
    target: Path,
    *,
    expected_guild: str,
    expected_application: str | None = None,
    allowed_root: Path | None = None,
    bot_id: str | None = None,
    home_channel: str | None = None,
    profile_id: str | None = None,
    expected_os_id: str | None = None,
    expected_os_version: str | None = None,
    finalizer: Callable = finalize_profile,
    preflight: Callable = preflight_profile,
    rollback_guard: Callable = ensure_profile_quiescent,
) -> dict:
    value = str(secret or "").strip()
    if not value or len(value) > 4096:
        raise ValueError("invalid credential length")
    raw_root = Path(allowed_root or Path.home() / ".hermes").expanduser().absolute()
    raw_target = Path(target).expanduser().absolute()
    if raw_root.is_symlink() or raw_target.is_symlink():
        raise ValueError("unsafe Station vault path")
    for path in (raw_root, raw_target.parent):
        if not path.is_dir() or path.stat().st_uid != os.geteuid() or path.stat().st_gid != os.getegid():
            raise ValueError("Station vault has the wrong owner")
    if os.path.lexists(raw_target):
        st = raw_target.stat()
        if not raw_target.is_file() or st.st_uid != os.geteuid() or st.st_gid != os.getegid():
            raise ValueError("credential target has the wrong owner")
    root = raw_root.resolve()
    target = raw_target.resolve()
    if not _inside(target, root):
        raise ValueError("target escapes the isolated Station vault")
    key = "DISCORD_BOT_TOKEN"
    if bot_id is not None:
        key = "DISCORD_BOT_" + canonical_bot_id(bot_id).replace("-", "_").upper() + "_TOKEN"
    identity = validate_discord_token(value)
    if expected_application is not None and str(identity.get("application_id") or "") != str(expected_application):
        raise ValueError("bot does not match the expected application")
    if str(expected_guild) not in set(identity.get("guilds") or []):
        raise ValueError("bot does not have access to the exact guild")
    if profile_id is not None or home_channel is not None:
        if not profile_id or not home_channel or not expected_os_id or not expected_os_version:
            raise ValueError("profile, OS identity, version, and home channel are required together")
        if profile_id != expected_os_id:
            raise ValueError("profile and OS identity must match")
        _validate_private_distribution(target.parent, expected_os_id, expected_os_version)
        preflight(profile_root=target.parent, profile_id=profile_id)
    previous = _snapshot_file(target)
    current = previous.content.decode("utf-8").splitlines() if previous is not None else []
    updates = {key: value}
    if home_channel is not None:
        if not _SNOWFLAKE.fullmatch(str(home_channel)):
            raise ValueError("invalid Discord home channel")
        updates.update({
            "DISCORD_HOME_CHANNEL": str(home_channel),
            "DISCORD_ALLOWED_CHANNELS": str(home_channel),
            "DISCORD_FREE_RESPONSE_CHANNELS": str(home_channel),
            "DISCORD_REQUIRE_MENTION": "true",
            "DISCORD_MESSAGE_CONTENT_INTENT": "true",
            "DISCORD_ALLOW_ALL_USERS": "false",
            "DISCORD_ALLOW_BOTS": "mentions",
            "DISCORD_BOTS_REQUIRE_INLINE_MENTION": "true",
        })
    output = _replace_env_values(current, updates)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.discord-token-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(output) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        _fsync_directory(target.parent)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
    result = {"id": identity["id"], "username": identity["username"], "guild_id": str(expected_guild)}
    if identity.get("application_id"):
        result["application_id"] = str(identity["application_id"])
    if profile_id is not None or home_channel is not None:
        try:
            finalizer(
                profile_root=target.parent,
                profile_id=profile_id,
                home_channel=str(home_channel),
                application_id=str(identity.get("application_id") or ""),
                guild_id=str(expected_guild),
            )
        except Exception as exc:
            if isinstance(exc, CredentialActivationUncertain):
                raise
            try:
                rollback_guard(profile_root=target.parent, profile_id=profile_id)
            except Exception as guard_error:
                raise CredentialActivationUncertain(
                    "submitted credential may still be active; environment was not hidden by rollback"
                ) from guard_error
            try:
                _restore_file(target, previous)
            except Exception as restore_error:
                raise CredentialActivationUncertain(
                    "submitted credential rollback could not be verified"
                ) from restore_error
            raise exc
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--allowed-root", type=Path)
    parser.add_argument("--expected-guild", required=True)
    parser.add_argument("--expected-application")
    parser.add_argument("--bot-id", type=canonical_bot_id)
    parser.add_argument("--profile-id", type=canonical_bot_id)
    parser.add_argument("--home-channel")
    parser.add_argument("--expected-os-id", type=canonical_bot_id)
    parser.add_argument("--expected-os-version")
    args = parser.parse_args()
    secret = os.read(0, 8193).decode("utf-8", "strict")
    if len(secret.encode()) > 8192:
        raise SystemExit("credential rejected")
    try:
        result = install_token(
            secret,
            args.target,
            expected_guild=args.expected_guild,
            expected_application=args.expected_application,
            allowed_root=args.allowed_root,
            bot_id=args.bot_id,
            profile_id=args.profile_id,
            home_channel=args.home_channel,
            expected_os_id=args.expected_os_id,
            expected_os_version=args.expected_os_version,
        )
    except ValueError:
        print(json.dumps({"status": "REJECTED"}))
        return 1
    result["invite_url"] = invite_url(result["application_id"])
    print(json.dumps({"status": "INSTALLED", **result}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
