#!/usr/bin/env python3
"""Build a bounded, redacted AGK Fleet operational snapshot.

Runs as root from a system timer because Linux homes remain mode 0700. The
result contains operational metadata only — never prompts, message bodies,
credentials, command payloads, filesystem paths, or private memories.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import yaml

ORGANISATIONS = ("operator", "agentik", "mission", "private")
STATUSES = ("triage", "todo", "scheduled", "ready", "running", "review", "blocked", "done")
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
TERMINAL_RUNTIME_STATUSES = {"archived", "completed", "failed", "stopped", "cancelled"}
_PROFILE_NAME_OVERRIDES = {
    "agk": "AGK", "os": "OS", "vat": "VAT", "youtube": "YouTube",
    "oto100m": "OTO100M",
}
_PROFILE_DISPLAY_OVERRIDES = {
    "clientdentistrygptee881c": "DentistryGPT Client",
}


def _text(value: Any, limit: int = 240) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _safe_id(value: Any) -> str:
    candidate = _text(value, 120).lower()
    return candidate if _ID.fullmatch(candidate) else ""


def _canonical_profile_name(profile_id: str) -> str:
    if profile_id in _PROFILE_DISPLAY_OVERRIDES:
        return _PROFILE_DISPLAY_OVERRIDES[profile_id]
    return " ".join(
        _PROFILE_NAME_OVERRIDES.get(part, part.title())
        for part in profile_id.split("-") if part
    )


def _profile_metadata(profile_dir: Path, profile_id: str) -> tuple[str, str]:
    try:
        raw = yaml.safe_load((profile_dir / "profile.yaml").read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    name = _text(raw.get("display_name"), 160) or _canonical_profile_name(profile_id)
    description = _text(raw.get("description"), 320)
    return name, description


def _env_key_configured(path: Path, key: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return False
    prefix = key + "="
    return any(line.startswith(prefix) and bool(line[len(prefix):].strip()) for line in lines)


def _env_values(path: Path, keys: set[str]) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return {}
    values: dict[str, str] = {}
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, value = line.split("=", 1)
        key = key.removeprefix("export ").strip()
        if key in keys:
            values[key] = value.strip()
    return values


def _discord_profile_state(hermes: Path, profile_id: str) -> dict[str, Any]:
    if not profile_id or profile_id == "default":
        return {
            "dedicated": False, "status": "profile_required",
            "token_configured": False, "service_installed": False,
            "gateway_connected": False, "service": "", "channel_id": "", "application_id": "",
            "owner_locked": False, "channel_access": False,
            "e2e_verified": False, "os_access": False, "ready": False,
        }
    profile_dir = hermes / "profiles" / profile_id
    home = hermes.parent
    service = f"hermes-gateway-{profile_id}.service"
    user_unit = home / ".config" / "systemd" / "user" / service
    system_unit = Path("/etc/systemd/system") / service
    service_installed = user_unit.is_file() or system_unit.is_file()
    token_configured = _env_key_configured(profile_dir / ".env", "DISCORD_BOT_TOKEN")
    routing = _env_values(profile_dir / ".env", {
        "DISCORD_ALLOWED_USERS", "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_FREE_RESPONSE_CHANNELS", "DISCORD_HOME_CHANNEL",
    })
    owner_id = os.environ.get("AGK_DISCORD_OWNER_ID", "1441423462492016821")
    channel_values = [routing.get(key, "") for key in (
        "DISCORD_ALLOWED_CHANNELS", "DISCORD_FREE_RESPONSE_CHANNELS", "DISCORD_HOME_CHANNEL",
    )]
    channel_id = channel_values[0] if channel_values and len(set(channel_values)) == 1 and channel_values[0].isdigit() else ""
    owner_locked = routing.get("DISCORD_ALLOWED_USERS", "") == owner_id
    connected = False
    try:
        state = json.loads((profile_dir / "gateway_state.json").read_text(encoding="utf-8"))
        platform = state.get("platforms", {}).get("discord", {}) if isinstance(state, dict) else {}
        pid = int(state.get("pid") or 0) if isinstance(state, dict) else 0
        writer_pid = int(platform.get("writer_pid") or 0) if isinstance(platform, dict) else 0
        start_time = int(state.get("start_time") or 0) if isinstance(state, dict) else 0
        writer_start_time = int(platform.get("writer_start_time") or 0) if isinstance(platform, dict) else 0
        proc_stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8") if pid > 0 else ""
        close = proc_stat.rfind(")")
        proc_start_time = int(proc_stat[close + 2:].split()[19]) if close >= 0 else 0
        connected = bool(
            isinstance(platform, dict)
            and platform.get("state") == "connected"
            and pid > 0
            and writer_pid == pid
            and Path(f"/proc/{pid}").is_dir()
            and start_time > 0
            and writer_start_time == start_time
            and proc_start_time == start_time
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        connected = False
    status = (
        "connected" if connected
        else "owner_required" if not token_configured
        else "service_required" if not service_installed
        else "configured"
    )
    receipt_ready = False
    receipt_channel = ""
    application_id = ""
    for receipt_name in ("nutrition-cutover-receipt.json", "discord-install-receipt.json", "discord-runtime-receipt.json"):
        try:
            receipt = json.loads((profile_dir / receipt_name).read_text(encoding="utf-8"))
            e2e = receipt.get("e2e") if isinstance(receipt, dict) else None
            exact_reply = bool(isinstance(e2e, dict) and e2e.get("exact_reply"))
            receipt_ready = bool(receipt.get("readiness", exact_reply)) and exact_reply
            receipt_channel = str(receipt.get("home_channel") or "")
            application_id = str(receipt.get("application_id") or application_id)
            if receipt_ready:
                break
        except (OSError, ValueError, TypeError):
            continue
    channel_access = bool(connected and channel_id and receipt_ready and receipt_channel == channel_id)
    return {
        "dedicated": True, "status": status,
        "token_configured": token_configured,
        "service_installed": service_installed,
        "gateway_connected": connected,
        "service": service,
        "channel_id": channel_id,
        "application_id": application_id,
        "owner_locked": owner_locked,
        "channel_access": channel_access,
        "e2e_verified": channel_access,
        "os_access": False,
        "ready": False,
    }


def _connect(path: Path) -> sqlite3.Connection | None:
    if not path.is_file() or path.is_symlink():
        return None
    try:
        connection = sqlite3.connect(
            f"file:{quote(str(path), safe='/')}?mode=ro", uri=True, timeout=1,
        )
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error:
        return None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not re.fullmatch(r"[a-z_]+", table):
        return set()
    try:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _select_existing(columns: set[str], requested: Iterable[str]) -> list[str]:
    return [column for column in requested if column in columns]


def _boards(hermes: Path) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    root = hermes / "kanban"
    try:
        current = _safe_id((root / "current").read_text(encoding="utf-8")) or "default"
    except OSError:
        current = "default"
    board_dirs: list[tuple[str, Path]] = [("default", root / "boards" / "default")]
    boards_root = root / "boards"
    if boards_root.is_dir() and not boards_root.is_symlink():
        for path in sorted(boards_root.iterdir()):
            slug = _safe_id(path.name)
            if slug and slug != "default" and path.is_dir() and not path.is_symlink():
                board_dirs.append((slug, path))

    board_rows: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    for slug, directory in board_dirs:
        metadata_path = directory / "board.json"
        metadata: dict[str, Any] = {}
        try:
            raw = json.loads(metadata_path.read_text(encoding="utf-8"))
            metadata = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            if slug != "default":
                continue
        db_path = hermes / "kanban.db" if slug == "default" else directory / "kanban.db"
        connection = _connect(db_path)
        board_tasks: list[dict[str, Any]] = []
        if connection is not None:
            try:
                columns = _columns(connection, "tasks")
                selected = _select_existing(columns, (
                    "id", "title", "assignee", "status", "priority", "created_at",
                    "started_at", "completed_at", "session_id", "project_id", "block_kind",
                ))
                required = {"id", "title", "status"}
                if required.issubset(selected):
                    order = "created_at" if "created_at" in columns else "id"
                    rows = connection.execute(
                        f"SELECT {', '.join(selected)} FROM tasks ORDER BY {order} DESC LIMIT 300"
                    ).fetchall()
                    for row in rows:
                        status = _text(row["status"], 32).lower()
                        task_id = _safe_id(row["id"])
                        title = _text(row["title"], 240)
                        if not task_id or not title or status not in STATUSES:
                            continue
                        item = {
                            "id": task_id,
                            "board": slug,
                            "title": title,
                            "assignee": _safe_id(row["assignee"]) if "assignee" in row.keys() else "",
                            "status": status,
                            "priority": int(row["priority"] or 0) if "priority" in row.keys() else 0,
                            "created_at": int(row["created_at"] or 0) if "created_at" in row.keys() else 0,
                            "started_at": int(row["started_at"] or 0) if "started_at" in row.keys() else 0,
                            "completed_at": int(row["completed_at"] or 0) if "completed_at" in row.keys() else 0,
                            "session_id": _safe_id(row["session_id"]) if "session_id" in row.keys() else "",
                            "project_id": _safe_id(row["project_id"]) if "project_id" in row.keys() else "",
                            "block_kind": _text(row["block_kind"], 40) if "block_kind" in row.keys() else "",
                        }
                        board_tasks.append(item)
                        tasks.append(item)
            except sqlite3.Error:
                board_tasks = []
            finally:
                connection.close()
        counts = {status: 0 for status in STATUSES}
        for task in board_tasks:
            counts[task["status"]] += 1
        board_rows.append({
            "slug": slug,
            "name": _text(metadata.get("name"), 100) or ("Default" if slug == "default" else slug.replace("-", " ").title()),
            "description": _text(metadata.get("description"), 300),
            "icon": _text(metadata.get("icon"), 8),
            "color": _text(metadata.get("color"), 16),
            "current": slug == current,
            "counts": counts,
            "task_count": len(board_tasks),
        })
    tasks.sort(key=lambda item: (item["created_at"], item["id"]), reverse=True)
    return current, board_rows, tasks[:500]


def _sessions(hermes: Path) -> list[dict[str, Any]]:
    connection = _connect(hermes / "state.db")
    if connection is None:
        return []
    try:
        columns = _columns(connection, "sessions")
        wanted = _select_existing(columns, (
            "id", "title", "display_name", "parent_session_id", "source", "model", "started_at", "ended_at",
            "last_activity_at", "message_count", "tool_call_count", "archived",
            "hidden", "profile_name",
        ))
        if not {"id", "source", "started_at"}.issubset(wanted):
            return []
        clauses = []
        if "archived" in columns:
            clauses.append("archived = 0")
        if "hidden" in columns:
            clauses.append("hidden = 0")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "last_activity_at" if "last_activity_at" in columns else "started_at"
        rows = connection.execute(
            f"SELECT {', '.join(wanted)} FROM sessions{where} ORDER BY {order} DESC LIMIT 80"
        ).fetchall()
        parent_ids = {
            _safe_id(row["parent_session_id"])
            for row in rows if "parent_session_id" in row.keys() and row["parent_session_id"]
        }
        parent_titles: dict[str, str] = {}
        if parent_ids:
            parent_columns = ["id"]
            if "title" in columns:
                parent_columns.append("title")
            if "display_name" in columns:
                parent_columns.append("display_name")
            placeholders = ",".join("?" for _ in parent_ids)
            for parent in connection.execute(
                f"SELECT {', '.join(parent_columns)} FROM sessions WHERE id IN ({placeholders})",
                tuple(sorted(parent_ids)),
            ).fetchall():
                parent_id = _safe_id(parent["id"])
                parent_title = (
                    _text(parent["title"], 180) if "title" in parent.keys() else ""
                ) or (
                    _text(parent["display_name"], 180) if "display_name" in parent.keys() else ""
                )
                if parent_id and parent_title:
                    parent_titles[parent_id] = parent_title
        result = []
        for row in rows:
            session_id = _safe_id(row["id"])
            if not session_id:
                continue
            source = _text(row["source"], 40)
            profile = _safe_id(row["profile_name"]) if "profile_name" in row.keys() else ""
            title = (_text(row["title"], 180) if "title" in row.keys() else "") or (
                _text(row["display_name"], 180) if "display_name" in row.keys() else ""
            )
            parent_id = _safe_id(row["parent_session_id"]) if "parent_session_id" in row.keys() else ""
            source_label = {
                "subagent": "Subagent", "cron": "Cron", "discord": "Discord",
                "cli": "CLI", "tool": "Tool session",
            }.get(source, source.title() or "Hermes session")
            if not title and parent_id in parent_titles:
                title = f"{source_label} · {parent_titles[parent_id]}"
            if not title:
                started = float(row["started_at"] or 0)
                timestamp = time.strftime("%d %b %H:%M", time.gmtime(started)) if started else ""
                title = f"{source_label} · {timestamp}" if timestamp else source_label
            result.append({
                "id": session_id,
                "title": title,
                "source": source,
                "model": _text(row["model"], 100) if "model" in row.keys() else "",
                "profile": profile or "default",
                "started_at": float(row["started_at"] or 0),
                "last_activity_at": float(row["last_activity_at"] or 0) if "last_activity_at" in row.keys() else 0,
                "active": not bool(row["ended_at"]) if "ended_at" in row.keys() else True,
                "message_count": int(row["message_count"] or 0) if "message_count" in row.keys() else 0,
                "tool_call_count": int(row["tool_call_count"] or 0) if "tool_call_count" in row.keys() else 0,
            })
        return result
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _runtimes(home: Path, organisation: str) -> list[dict[str, Any]]:
    connection = _connect(home / ".agentik" / "runtime.db")
    if connection is None:
        return []
    try:
        columns = _columns(connection, "runtime_sessions")
        wanted = _select_existing(columns, (
            "id", "name", "type", "environment", "status", "last_activity",
            "archived_at", "hermes_profile",
        ))
        if not {"id", "name", "type", "environment", "status"}.issubset(wanted):
            return []
        where = "environment = ?"
        if "archived_at" in columns:
            where += " AND archived_at IS NULL"
        order = "last_activity" if "last_activity" in columns else "id"
        rows = connection.execute(
            f"SELECT {', '.join(wanted)} FROM runtime_sessions WHERE {where} ORDER BY {order} DESC LIMIT 80",
            (organisation,),
        ).fetchall()
        result = []
        for row in rows:
            runtime_id = _safe_id(row["id"])
            name = _text(row["name"], 120)
            if not runtime_id or not name:
                continue
            status = _text(row["status"], 40) or "unknown"
            result.append({
                "id": runtime_id, "name": name, "type": _text(row["type"], 40),
                "status": status, "active": status not in TERMINAL_RUNTIME_STATUSES,
                "last_activity": float(row["last_activity"] or 0) if "last_activity" in row.keys() else 0,
                "profile": _safe_id(row["hermes_profile"]) if "hermes_profile" in row.keys() else "",
            })
        return result
    except sqlite3.Error:
        return []
    finally:
        connection.close()


def _agents(hermes: Path, organisation: str) -> list[dict[str, Any]]:
    root = hermes / "agents"
    if not root.is_dir() or root.is_symlink():
        return []
    result = []
    for path in sorted(root.glob("*/agent.yaml")):
        if path.is_symlink():
            continue
        try:
            if path.stat().st_size > 1024 * 1024:
                continue
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        if not isinstance(raw, dict):
            continue
        agent_id = _safe_id(raw.get("id"))
        raw_scopes = raw.get("scope")
        scopes: list[Any] = raw_scopes if isinstance(raw_scopes, list) else []
        allowed = "global" in scopes or organisation in scopes
        if not agent_id or not allowed:
            continue
        prompt_name = _text(raw.get("prompt") or "prompt.md", 120)
        prompt_present = (path.parent / prompt_name).is_file() and "/" not in prompt_name and ".." not in prompt_name
        raw_os = raw.get("os")
        os_items: list[Any] = raw_os if isinstance(raw_os, list) else []
        profile_id = _safe_id(raw.get("profile"))
        result.append({
            "id": agent_id, "name": _text(raw.get("name"), 160) or agent_id,
            "version": _text(raw.get("version"), 40),
            "description": _text(raw.get("description"), 320),
            "scope": [str(scope) for scope in scopes if isinstance(scope, str)][:8],
            "runtime": _text(raw.get("runtime"), 60),
            "profile": profile_id,
            "os": [_text(item, 120) for item in os_items if isinstance(item, str)][:20],
            "ready": prompt_present,
            "discord": _discord_profile_state(hermes, profile_id),
        })
    known = {item["id"] for item in result}
    claimed_profiles = {item["profile"] for item in result if item.get("profile")}
    profiles_root = hermes / "profiles"
    if profiles_root.is_dir() and not profiles_root.is_symlink():
        for profile_dir in sorted(profiles_root.iterdir()):
            profile_id = _safe_id(profile_dir.name)
            if (
                not profile_id
                or profile_id in known
                or profile_id in claimed_profiles
                or not profile_dir.is_dir()
                or profile_dir.is_symlink()
                or not (profile_dir / "config.yaml").is_file()
            ):
                continue
            name, description = _profile_metadata(profile_dir, profile_id)
            result.append({
                "id": profile_id,
                "name": name,
                "version": "profile",
                "description": description or f"Profil Hermes {name} isolé pour cette station.",
                "scope": [organisation],
                "runtime": "hermes-profile",
                "profile": profile_id,
                "os": [],
                "ready": True,
                "discord": _discord_profile_state(hermes, profile_id),
            })
    result.sort(key=lambda item: (item["name"].casefold(), item["id"]))
    return result


def _os_packages(registry_root: Path, organisation: str) -> list[dict[str, Any]]:
    index = registry_root / "state" / "index.json"
    try:
        raw = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    packages = raw.get("packages") if isinstance(raw, dict) else []
    if not isinstance(packages, list):
        return []
    result = []
    for package in packages:
        if not isinstance(package, dict):
            continue
        package_id = _safe_id(package.get("id"))
        version = _text(package.get("version"), 60)
        raw_scopes = package.get("scope")
        scopes: list[Any] = raw_scopes if isinstance(raw_scopes, list) else []
        if not package_id or not version or not ("global" in scopes or organisation in scopes):
            continue
        package_dir = registry_root / "packages" / package_id / version
        try:
            installed = package_dir.is_dir() and not package_dir.is_symlink() and package_dir.resolve().is_relative_to((registry_root / "packages").resolve())
        except (OSError, RuntimeError, ValueError):
            installed = False
        raw_agents = package.get("agents")
        package_agents: list[Any] = raw_agents if isinstance(raw_agents, list) else []
        result.append({
            "id": package_id, "name": _text(package.get("name"), 180) or package_id,
            "version": version, "description": _text(package.get("description"), 320),
            "scope": [str(scope) for scope in scopes if isinstance(scope, str)][:8],
            "agents": [_text(item, 120) for item in package_agents if isinstance(item, str)][:40],
            "skills": len(package.get("skills", [])) if isinstance(package.get("skills"), list) else 0,
            "workflows": len(package.get("workflows", [])) if isinstance(package.get("workflows"), list) else 0,
            "tools": len(package.get("tools", [])) if isinstance(package.get("tools"), list) else 0,
            "installed": installed,
        })
    result.sort(key=lambda item: (item["name"].casefold(), item["version"]))
    return result


def _apply_assignments(home: Path, organisation: str, packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assignment_paths = [home / ".agentik" / "os-assignments.yaml"]
    if organisation == "operator":
        assignment_paths.append(Path("/etc/agentik/operator-os/assignments.yaml"))
    references: set[str] = set()
    for path in assignment_paths:
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, UnicodeError, yaml.YAMLError):
            continue
        rows = document.get("assignments") if isinstance(document, dict) else []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("target") != organisation:
                continue
            reference = _text(row.get("os"), 180)
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}@[0-9][0-9A-Za-z.+-]{0,59}", reference):
                references.add(reference)
    by_reference = {f"{item['id']}@{item['version']}": item for item in packages}
    for reference in references:
        if reference in by_reference:
            by_reference[reference]["assigned"] = True
            continue
        package_id, version = reference.split("@", 1)
        packages.append({
            "id": package_id,
            "name": package_id.replace("-", " ").title(),
            "version": version,
            "description": "Assignment déclarée mais package absent du registre canonique.",
            "scope": [organisation],
            "agents": [],
            "skills": 0,
            "workflows": 0,
            "tools": 0,
            "installed": False,
            "assigned": True,
        })
    for package in packages:
        package.setdefault("assigned", False)
    packages.sort(key=lambda item: (item["name"].casefold(), item["version"]))
    return packages


def collect_snapshot(*, homes: dict[str, Path], registry_root: Path, now: int | None = None) -> dict[str, Any]:
    generated_at = int(time.time() if now is None else now)
    organisations: dict[str, Any] = {}
    for organisation, home in homes.items():
        hermes = home / ".hermes"
        current, boards, tasks = _boards(hermes)
        counts = {status: 0 for status in STATUSES}
        for task in tasks:
            counts[task["status"]] += 1
        sessions = _sessions(hermes)
        runtimes = _runtimes(home, organisation)
        agents = _agents(hermes, organisation)
        packages = _apply_assignments(home, organisation, _os_packages(registry_root, organisation))
        package_by_id = {str(item.get("id") or ""): item for item in packages}
        for agent in agents:
            raw_discord = agent.get("discord")
            discord: dict[str, Any] = raw_discord if isinstance(raw_discord, dict) else {}
            expected_os = [str(item).split("@", 1)[0] for item in agent.get("os", []) if str(item)]
            if not expected_os and str(agent.get("profile") or "") in package_by_id:
                expected_os = [str(agent["profile"])]
            package_access = bool(expected_os) and all(
                bool(package_by_id.get(item))
                and bool(package_by_id[item].get("installed"))
                and (bool(package_by_id[item].get("assigned")) or bool(discord.get("e2e_verified")))
                for item in expected_os
            )
            os_access = bool(agent.get("ready") and agent.get("profile") and package_access and discord.get("e2e_verified"))
            discord["os_access"] = os_access
            discord["ready"] = bool(
                discord.get("gateway_connected") and discord.get("owner_locked")
                and discord.get("channel_access") and discord.get("e2e_verified") and os_access
            )
            agent["discord"] = discord
        profiles_root = hermes / "profiles"
        profiles = ["default"]
        if profiles_root.is_dir() and not profiles_root.is_symlink():
            profiles.extend(sorted(
                path.name for path in profiles_root.iterdir()
                if path.is_dir() and not path.is_symlink() and _safe_id(path.name)
            ))
        organisations[organisation] = {
            "id": organisation,
            "healthy": hermes.is_dir(),
            "profiles": profiles,
            "kanban": {"current_board": current, "boards": boards, "tasks": tasks, "counts": counts},
            "sessions": sessions,
            "runtimes": runtimes,
            "agents": agents,
            "os": packages,
            "summary": {
                "active_sessions": sum(1 for item in sessions if item["active"]),
                "active_runtimes": sum(1 for item in runtimes if item["active"]),
                "open_tasks": sum(counts[status] for status in STATUSES if status != "done"),
                "blocked_tasks": counts["blocked"],
                "agent_count": len(agents),
                "os_count": sum(1 for item in packages if item["installed"]),
            },
        }
    return {"schema": "agk.fleet.v1", "generated_at": generated_at, "organisations": organisations}


def _read_at(directory_fd: int, name: str, owner_uid: int, *, max_bytes: int = 2 * 1024 * 1024) -> tuple[bool, str, int, int, int]:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        directory_stat = os.fstat(directory_fd)
        return False, "", 0o600, directory_stat.st_uid, directory_stat.st_gid
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != owner_uid or metadata.st_size > max_bytes:
            raise ValueError("managed routing file is unsafe")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > max_bytes:
            raise ValueError("managed routing file is too large")
        return True, payload.decode("utf-8"), stat.S_IMODE(metadata.st_mode), metadata.st_uid, metadata.st_gid
    finally:
        os.close(fd)


def _write_at_atomic(directory_fd: int, name: str, content: str, mode: int, uid: int, gid: int) -> None:
    temporary = f".{name}.{os.getpid()}.{time.time_ns()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory_fd)
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, uid, gid)
        payload = content.encode("utf-8")
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    try:
        os.rename(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    except Exception:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except OSError:
            pass
        raise


def _restore_at(directory_fd: int, name: str, original: tuple[bool, str, int, int, int]) -> None:
    existed, content, mode, uid, gid = original
    if existed:
        _write_at_atomic(directory_fd, name, content, mode, uid, gid)
    else:
        try:
            os.unlink(name, dir_fd=directory_fd)
            os.fsync(directory_fd)
        except FileNotFoundError:
            pass


def _open_owned_dir(name: str, *, parent_fd: int | None, owner_uid: int) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    if os.fstat(fd).st_uid != owner_uid:
        os.close(fd)
        raise ValueError("routing directory ownership is invalid")
    return fd


def _read_request_payload(request_fd: int, request_name: str, operator_uid: int) -> dict[str, Any]:
    descriptor = os.open(request_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=request_fd)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != operator_uid or metadata.st_size > 16384:
            raise ValueError("untrusted routing request file")
        payload = os.read(descriptor, 16385)
        if len(payload) > 16384:
            raise ValueError("routing request is too large")
        raw = json.loads(payload.decode("utf-8"))
    finally:
        os.close(descriptor)
    if not isinstance(raw, dict):
        raise ValueError("invalid request payload")
    return raw


def _apply_routing_request(request_fd: int, request_name: str, homes: dict[str, Path], owner_id: str) -> None:
    raw = _read_request_payload(request_fd, request_name, homes["operator"].stat().st_uid)
    if not isinstance(raw, dict) or raw.get("schema") != "agk.agent-discord-routing.v1":
        raise ValueError("invalid routing request schema")
    organisation = str(raw.get("organisation") or "")
    profile = str(raw.get("profile") or "")
    channel_id = str(raw.get("channel_id") or "")
    application_id = str(raw.get("application_id") or "")
    if organisation not in homes or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", profile):
        raise ValueError("invalid routing target")
    if not re.fullmatch(r"\d{17,20}", channel_id) or not re.fullmatch(r"\d{17,20}", application_id) or str(raw.get("owner_id") or "") != owner_id:
        raise ValueError("invalid owner, application or channel")

    home_fd = os.open(homes[organisation], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    station_uid = os.fstat(home_fd).st_uid
    hermes_fd = profiles_fd = profile_fd = -1
    try:
        hermes_fd = _open_owned_dir(".hermes", parent_fd=home_fd, owner_uid=station_uid)
        profiles_fd = _open_owned_dir("profiles", parent_fd=hermes_fd, owner_uid=station_uid)
        profile_fd = _open_owned_dir(profile, parent_fd=profiles_fd, owner_uid=station_uid)
        originals = {
            ".env": _read_at(profile_fd, ".env", station_uid),
            "config.yaml": _read_at(profile_fd, "config.yaml", station_uid),
            "discord-routing-receipt.json": _read_at(profile_fd, "discord-routing-receipt.json", station_uid),
        }
        config = yaml.safe_load(originals["config.yaml"][1]) if originals["config.yaml"][0] else {}
        if not isinstance(config, dict):
            raise ValueError("profile config is invalid")
        platforms = config.setdefault("platforms", {})
        if not isinstance(platforms, dict):
            raise ValueError("profile platform config is invalid")
        discord = platforms.setdefault("discord", {})
        if not isinstance(discord, dict):
            raise ValueError("Discord config is invalid")
        discord["enabled"] = True
        discord["gateway_restart_notification"] = False
        extra = discord.setdefault("extra", {})
        if not isinstance(extra, dict):
            extra = {}
            discord["extra"] = extra
        extra["bots_require_inline_mention"] = True

        current = originals[".env"][1].splitlines()
        managed = {
            "DISCORD_ALLOWED_USERS": owner_id,
            "DISCORD_ALLOWED_CHANNELS": channel_id,
            "DISCORD_FREE_RESPONSE_CHANNELS": channel_id,
            "DISCORD_HOME_CHANNEL": channel_id,
        }
        output: list[str] = []
        seen: set[str] = set()
        for line in current:
            candidate = line.removeprefix("export ")
            key = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
            if key in managed:
                if key not in seen:
                    output.append(f"{key}={managed[key]}")
                    seen.add(key)
                continue
            output.append(line)
        for key, value in managed.items():
            if key not in seen:
                output.append(f"{key}={value}")
        receipt = {
            "schema": "agk.agent-discord-routing-receipt.v1",
            "profile": profile, "organisation": organisation,
            "owner_id": owner_id, "application_id": application_id, "channel_id": channel_id,
            "applied_at": int(time.time()), "ready": False,
        }
        writes = {
            ".env": "\n".join(output) + "\n",
            "config.yaml": yaml.safe_dump(config, sort_keys=False),
            "discord-routing-receipt.json": json.dumps(receipt, separators=(",", ":")) + "\n",
        }
        try:
            for name, content in writes.items():
                original = originals[name]
                _write_at_atomic(profile_fd, name, content, original[2], original[3], original[4])
        except Exception:
            rollback_errors = []
            for name, original in originals.items():
                try:
                    _restore_at(profile_fd, name, original)
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if rollback_errors:
                raise RuntimeError("routing transaction failed and rollback was incomplete")
            raise
    finally:
        for fd in (profile_fd, profiles_fd, hermes_fd, home_fd):
            if fd >= 0:
                os.close(fd)


def _launch_secure_input_request(request_fd: int, request_name: str, request_id: str, homes: dict[str, Path], owner_id: str, status_root: Path) -> None:
    raw = _read_request_payload(request_fd, request_name, homes["operator"].stat().st_uid)
    if raw.get("schema") != "agk.agent-discord-secure-input.v1":
        raise ValueError("invalid secure input request schema")
    organisation = str(raw.get("organisation") or "")
    profile = str(raw.get("profile") or "")
    application_id = str(raw.get("application_id") or "")
    channel_id = str(raw.get("channel_id") or "")
    guild_id = str(raw.get("guild_id") or "")
    expected_os_id = str(raw.get("expected_os_id") or "")
    expected_os_version = str(raw.get("expected_os_version") or "")
    if organisation not in homes or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", profile):
        raise ValueError("invalid secure input target")
    if any(not re.fullmatch(r"\d{17,20}", value) for value in (application_id, channel_id, guild_id)) or str(raw.get("owner_id") or "") != owner_id:
        raise ValueError("invalid secure input identity")
    if expected_os_id != profile or not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", expected_os_version):
        raise ValueError("invalid secure input OS identity")
    home = homes[organisation]
    profile_home = home / ".hermes" / "profiles" / profile
    if not profile_home.is_dir() or profile_home.is_symlink():
        raise ValueError("secure input profile is unavailable")
    status_root.mkdir(parents=True, exist_ok=True)
    os.chmod(status_root, 0o750)
    operator_stat = homes["operator"].stat()
    if os.geteuid() == 0:
        os.chown(status_root, 0, operator_stat.st_gid)
    status_path = status_root / f"{request_id}.jsonl"
    status_fd = os.open(status_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640)
    try:
        os.fchmod(status_fd, 0o640)
        if os.geteuid() == 0:
            os.fchown(status_fd, 0, operator_stat.st_gid)
        os.fsync(status_fd)
    finally:
        os.close(status_fd)
    installer = [
        "/usr/sbin/runuser", "-u", organisation, "--", "/usr/bin/env",
        f"HOME={home}", f"HERMES_HOME={profile_home}",
        f"XDG_RUNTIME_DIR=/run/user/{home.stat().st_uid}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{home.stat().st_uid}/bus",
        "PATH=/usr/local/bin:/usr/bin:/bin",
        "/usr/local/lib/agk-terminal/venv/bin/python",
        "/usr/local/lib/agk-terminal/scripts/install-discord-token.py",
        "--target", str(profile_home / ".env"),
        "--allowed-root", str(profile_home),
        "--expected-guild", guild_id,
        "--expected-application", application_id,
        "--profile-id", profile,
        "--home-channel", channel_id,
        "--expected-os-id", expected_os_id,
        "--expected-os-version", expected_os_version,
    ]
    uid = home.stat().st_uid
    unit = f"agk-secure-input-{request_id}"[:240]
    command = [
        "/usr/bin/systemd-run", "--no-block", "--collect", f"--unit={unit}",
        "--uid=root", "--gid=operator",
        f"--property=StandardOutput=append:{status_path}",
        f"--property=StandardError=append:{status_path}",
        "--property=PrivateTmp=true", "--property=NoNewPrivileges=true",
        f"--setenv=HOME={home}", f"--setenv=HERMES_HOME={profile_home}",
        f"--setenv=XDG_RUNTIME_DIR=/run/user/{uid}",
        f"--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
        "--setenv=PATH=/usr/local/bin:/usr/bin:/bin",
        "/usr/local/lib/agk-terminal/venv/bin/python",
        "/usr/local/lib/agk-terminal/scripts/tailnet_secure_input.py",
        "--installer-json", json.dumps(installer), "--ttl", "1800",
    ]
    subprocess.run(command, check=True, timeout=30, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def process_routing_requests(request_dir: Path, homes: dict[str, Path], owner_id: str, status_root: Path | None = None) -> tuple[int, int]:
    try:
        request_fd = os.open(request_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return 0, 0
    applied = failed = 0
    try:
        names = sorted(name for name in os.listdir(request_fd) if re.fullmatch(r"[0-9A-Za-z.-]+\.json", name))[:100]
        for name in names:
            try:
                raw = _read_request_payload(request_fd, name, homes["operator"].stat().st_uid)
                if raw.get("schema") == "agk.agent-discord-routing.v1":
                    _apply_routing_request(request_fd, name, homes, owner_id)
                elif raw.get("schema") == "agk.agent-discord-secure-input.v1" and status_root is not None:
                    _launch_secure_input_request(request_fd, name, name.removesuffix(".json"), homes, owner_id, status_root)
                else:
                    raise ValueError("unsupported Fleet request schema")
                os.unlink(name, dir_fd=request_fd)
                os.fsync(request_fd)
                applied += 1
            except Exception:
                try:
                    os.rename(name, name.removesuffix(".json") + ".failed", src_dir_fd=request_fd, dst_dir_fd=request_fd)
                    os.fsync(request_fd)
                except OSError:
                    pass
                failed += 1
    finally:
        os.close(request_fd)
    return applied, failed


def atomic_write(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, prefix=".fleet-snapshot.", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o640)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/var/lib/agk-terminal/fleet/fleet-snapshot.json")
    parser.add_argument("--registry", default="/opt/agentik/os-registry")
    parser.add_argument("--routing-requests", default="/run/user/1000/hermes-fleet-routing")
    parser.add_argument("--secure-status", default="/var/lib/agk-terminal/fleet/secure-input")
    args = parser.parse_args()
    homes = {organisation: Path("/home") / organisation for organisation in ORGANISATIONS}
    process_routing_requests(Path(args.routing_requests), homes, os.environ.get("AGK_DISCORD_OWNER_ID", "1441423462492016821"), Path(args.secure_status))
    snapshot = collect_snapshot(homes=homes, registry_root=Path(args.registry))
    atomic_write(Path(args.output), snapshot)
    print(f"AGK Fleet snapshot: {len(snapshot['organisations'])} station(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
