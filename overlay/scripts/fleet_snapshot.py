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
        result.append({
            "id": agent_id, "name": _text(raw.get("name"), 160) or agent_id,
            "version": _text(raw.get("version"), 40),
            "description": _text(raw.get("description"), 320),
            "scope": [str(scope) for scope in scopes if isinstance(scope, str)][:8],
            "runtime": _text(raw.get("runtime"), 60),
            "profile": _safe_id(raw.get("profile")),
            "os": [_text(item, 120) for item in os_items if isinstance(item, str)][:20],
            "ready": prompt_present,
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
    args = parser.parse_args()
    homes = {organisation: Path("/home") / organisation for organisation in ORGANISATIONS}
    snapshot = collect_snapshot(homes=homes, registry_root=Path(args.registry))
    atomic_write(Path(args.output), snapshot)
    print(f"AGK Fleet snapshot: {len(snapshot['organisations'])} station(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
