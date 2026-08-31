"""Sanitized Station-wide Hermes and AGK session catalog primitives."""
from __future__ import annotations

import hashlib
import argparse
import json
import pwd
import re
import sqlite3
import subprocess
import sys

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

MANAGER_CHANNEL_ID = 1542462952714670190
ENVIRONMENTS = frozenset({"operator", "agentik", "mission", "private", "collective"})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,119}$")
_SECRET_PATTERNS = (
    re.compile(r"(?i)(?:api[_-]?key|token|password|secret)\s*[=:]\s*[^\s]+"),
    re.compile(r"(?i)authorization\s*:\s*bearer\s+[^\s]+"),
    re.compile(r"\bsk-(?:or-v1-)?[A-Za-z0-9_-]{8,}\b"),
)


class ControlError(ValueError):
    """Raised for invalid or unauthorized control-plane inputs."""


@dataclass(frozen=True)
class PlanProgress:
    completed: int = 0
    total: int = 0
    percent: int = 0
    bar: str = "░░░░░░░░░░"


@dataclass(frozen=True)
class Target:
    kind: str
    environment: str
    identifier: str

    @property
    def raw(self) -> str:
        return f"{self.kind}:{self.environment}:{self.identifier}"


@dataclass
class SessionRecord:
    environment: str
    display_name: str
    status: str
    runtime_type: str = "hermes"
    runtime_id: str | None = None
    runtime_name: str | None = None
    rmux_session: str | None = None
    hermes_session_id: str | None = None
    profile: str = "default"
    title: str = ""
    cwd: str = ""
    last_activity: float = 0.0
    progress: PlanProgress = field(default_factory=PlanProgress)

    @property
    def can_prompt(self) -> bool:
        return bool(self.runtime_id and self.rmux_session and self.status not in {"archived", "stopped", "completed", "complete"})

    @property
    def can_stop(self) -> bool:
        return self.can_prompt


@dataclass(frozen=True)
class EnvironmentPaths:
    name: str
    home: Path
    runtime_db: Path
    hermes_profiles: list[tuple[str, Path]]


def channel_allowed(channel_id: int | str) -> bool:
    try:
        return int(channel_id) == MANAGER_CHANNEL_ID
    except (TypeError, ValueError):
        return False


def plan_progress(items: Iterable[dict]) -> PlanProgress:
    rows = [item for item in items if isinstance(item, dict)]
    total = len(rows)
    completed = sum(str(item.get("status") or "").lower() in {"completed", "cancelled"} for item in rows)
    percent = round(completed * 100 / total) if total else 0
    filled = round(percent / 10)
    return PlanProgress(completed, total, percent, "█" * filled + "░" * (10 - filled))


def progress_label(progress: PlanProgress) -> str:
    if not progress.total:
        return "No applied plan"
    return f"{progress.bar} {progress.percent}% · {progress.completed}/{progress.total}"


def parse_target(raw: str) -> Target:
    parts = str(raw or "").split(":", 2)
    if len(parts) != 3:
        raise ControlError("invalid session target")
    kind, environment, identifier = parts
    if kind not in {"runtime", "hermes"} or environment not in ENVIRONMENTS or not _ID.fullmatch(identifier):
        raise ControlError("invalid session target")
    return Target(kind, environment, identifier)


def confirmation_token(target: Target) -> str:
    return hashlib.sha256(("station-session:" + target.raw).encode()).hexdigest()[:16]


def require_confirmation(target: Target, token: str) -> None:
    if str(token or "") != confirmation_token(target):
        raise ControlError("confirmation token is required")


def redact_log(text: str, limit: int = 12000) -> str:
    bounded = max(1, min(limit, 40000))
    value = str(text or "")[-min(40000, bounded + 8192):]
    try:
        from agent.redact import redact_sensitive_text
        value = redact_sensitive_text(value, force=True)
    except Exception:
        pass
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value[-bounded:]


def _latest_progress(db: sqlite3.Connection, session_id: str) -> PlanProgress:
    try:
        rows = db.execute(
            "SELECT content FROM messages WHERE session_id=? AND tool_name='todo' "
            "AND COALESCE(active,1)=1 ORDER BY COALESCE(timestamp,0) DESC, id DESC LIMIT 20",
            (session_id,),
        ).fetchall()
    except sqlite3.Error:
        return PlanProgress()
    for row in rows:
        try:
            data = json.loads(row[0] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        items = data.get("todos") if isinstance(data, dict) else None
        if isinstance(items, list):
            return plan_progress(items)
    return PlanProgress()


def _hermes_rows(path: Path, profile: str) -> list[SessionRecord]:
    if not path.is_file():
        return []
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        rows = db.execute(
            "SELECT id,title,source,message_count,last_activity_at,started_at,archived,cwd,profile_name "
            "FROM sessions WHERE COALESCE(archived,0)=0 "
            "ORDER BY COALESCE(last_activity_at,started_at,0) DESC LIMIT 100"
        ).fetchall()
        return [SessionRecord(
            environment="",
            display_name=str(row["title"] or row["id"]),
            status="idle",
            hermes_session_id=str(row["id"]),
            profile=str(row["profile_name"] or profile),
            title=str(row["title"] or ""),
            cwd=str(row["cwd"] or ""),
            last_activity=float(row["last_activity_at"] or row["started_at"] or 0),
            progress=_latest_progress(db, str(row["id"])),
        ) for row in rows]
    finally:
        db.close()


def catalog_for_environment(
    environment: str,
    runtime_db: Path,
    hermes_profiles: list[tuple[str, Path]],
) -> list[SessionRecord]:
    if environment not in ENVIRONMENTS:
        raise ControlError("unknown environment")
    sessions: dict[str, SessionRecord] = {}
    for profile, path in hermes_profiles:
        for row in _hermes_rows(path, profile):
            row.environment = environment
            if row.hermes_session_id:
                sessions[row.hermes_session_id] = row
    if runtime_db.is_file():
        db = sqlite3.connect(f"file:{runtime_db}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            rows = db.execute(
                "SELECT id,name,type,environment,rmux_session,cwd,status,last_activity,hermes_session "
                "FROM runtime_sessions WHERE archived_at IS NULL AND environment=? ORDER BY last_activity DESC",
                (environment,),
            ).fetchall()
        finally:
            db.close()
        for raw in rows:
            linked = str(raw["hermes_session"] or "")
            record = sessions.pop(linked, None) if linked else None
            if record is None:
                record = SessionRecord(environment=environment, display_name=str(raw["name"]), status=str(raw["status"] or "unknown"))
            record.environment = environment
            record.display_name = str(raw["name"])
            record.status = str(raw["status"] or "unknown")
            record.runtime_type = str(raw["type"] or "unknown")
            record.runtime_id = str(raw["id"])
            record.runtime_name = str(raw["name"])
            record.rmux_session = str(raw["rmux_session"] or "") or None
            record.cwd = str(raw["cwd"] or record.cwd)
            record.last_activity = float(raw["last_activity"] or record.last_activity or 0)
            record.hermes_session_id = linked or record.hermes_session_id
            sessions[f"runtime:{record.runtime_id}"] = record
    return sorted(sessions.values(), key=lambda row: row.last_activity, reverse=True)


class StationSessionController:
    """Fixed-scope Station session operations; never accepts filesystem paths from Discord."""

    def __init__(self, environments: dict[str, EnvironmentPaths] | None = None, runner=None):
        self.environments = environments if environments is not None else default_environments()
        self.runner = runner or subprocess.run

    def _paths(self, environment: str) -> EnvironmentPaths:
        paths = self.environments.get(environment)
        if paths is None:
            raise ControlError("unknown environment")
        return paths

    def list_sessions(self) -> list[SessionRecord]:
        rows: list[SessionRecord] = []
        for environment, paths in self.environments.items():
            if paths.hermes_profiles:
                try:
                    rows.extend(catalog_for_environment(environment, paths.runtime_db, paths.hermes_profiles))
                    continue
                except (OSError, sqlite3.Error, PermissionError):
                    pass
            rows.extend(self._catalog_as_owner(paths))
        return sorted(rows, key=lambda row: row.last_activity, reverse=True)

    def _catalog_as_owner(self, paths: EnvironmentPaths) -> list[SessionRecord]:
        result = self._run_as_owner(
            paths,
            [sys.executable, str(Path(__file__).resolve()), "catalog", paths.name],
            timeout=45,
        )
        try:
            payload = json.loads(result.stdout or "[]")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ControlError("invalid catalog response") from exc
        records: list[SessionRecord] = []
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            progress = item.get("progress") or {}
            item["progress"] = PlanProgress(**progress) if isinstance(progress, dict) else PlanProgress()
            records.append(SessionRecord(**item))
        return records

    @staticmethod
    def _helper_path() -> Path:
        installed = Path("/opt/agk-terminal/hermes-agent/plugins/platforms/discord/agk_session_control.py")
        return installed if installed.is_file() else Path(__file__).resolve()

    @staticmethod
    def _local_to(paths: EnvironmentPaths) -> bool:
        if Path.home().resolve() == paths.home.resolve():
            return True
        try:
            if paths.runtime_db.is_file():
                return True
            return any(path.is_file() for _profile, path in paths.hermes_profiles)
        except PermissionError:
            return False

    def _run_helper(self, paths: EnvironmentPaths, action: str, *values: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
        return self._run_as_owner(
            paths,
            [sys.executable, str(self._helper_path()), action, paths.name, *values],
            timeout=timeout,
        )

    def _runtime_row(self, target: Target) -> sqlite3.Row:
        if target.kind != "runtime":
            raise ControlError("action requires a live AGK runtime")
        paths = self._paths(target.environment)
        if not paths.runtime_db.is_file():
            raise ControlError("runtime registry is unavailable")
        db = sqlite3.connect(f"file:{paths.runtime_db}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        try:
            row = db.execute(
                "SELECT * FROM runtime_sessions WHERE id=? AND archived_at IS NULL",
                (target.identifier,),
            ).fetchone()
        finally:
            db.close()
        if row is None or str(row["environment"] or "") != target.environment:
            raise ControlError("runtime target is unavailable")
        return row

    def runtime_action_argv(self, target: Target, action: str) -> list[str]:
        row = self._runtime_row(target)
        name = str(row["name"])
        mapping = {
            "stop": ["/usr/local/bin/agk", "kill", name, "--yes"],
            "archive": ["/usr/local/bin/agk", "close", name, "--yes"],
            "delete": ["/usr/local/bin/agk", "purge", name, "--yes"],
        }
        if action not in mapping:
            raise ControlError("unsupported runtime action")
        return mapping[action]

    @staticmethod
    def _owner(paths: EnvironmentPaths) -> str:
        return "agentik" if paths.name == "collective" else paths.name

    def _run_as_owner(self, paths: EnvironmentPaths, argv: list[str], timeout: int = 30, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        owner = self._owner(paths)
        try:
            uid = pwd.getpwnam(owner).pw_uid
        except KeyError as exc:
            raise ControlError("environment owner is unavailable") from exc
        command = [
            "sudo", "-n", "-u", owner, "env",
            f"HOME={paths.home}", f"XDG_RUNTIME_DIR=/run/user/{uid}",
            *argv,
        ]
        try:
            result = self.runner(command, text=True, input=input_text, capture_output=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ControlError("session action timed out") from exc
        if result.returncode:
            message = redact_log(result.stderr or result.stdout or "session action failed", 2000)
            raise ControlError(message)
        return result

    def validate_prompt(self, target: Target, prompt: str) -> str:
        if target.kind != "runtime":
            raise ControlError("prompt requires a live AGK runtime")
        value = str(prompt or "").strip()
        if not 1 <= len(value) <= 4000:
            raise ControlError("prompt must contain 1-4000 characters")
        return value

    def send_prompt(self, target: Target, prompt: str) -> None:
        value = self.validate_prompt(target, prompt)
        paths = self._paths(target.environment)
        if not self._local_to(paths):
            self._run_as_owner(
                paths,
                [sys.executable, str(self._helper_path()), "prompt", paths.name, target.raw],
                timeout=60,
                input_text=value,
            )
            return
        row = self._runtime_row(target)
        session = str(row["rmux_session"] or "")
        if not session:
            raise ControlError("runtime has no RMUX session")
        exact_session = session if session.startswith("=") else f"={session}"
        listed = self._run_as_owner(paths, ["rmux", "list-panes", "-t", exact_session, "-F", "#{pane_id}"])
        pane = next((line.strip() for line in listed.stdout.splitlines() if line.strip()), "")
        if not pane or not re.fullmatch(r"%[0-9]+", pane):
            raise ControlError("runtime has no live pane")
        self._run_as_owner(paths, ["rmux", "send-keys", "-t", pane, "-l", value])
        self._run_as_owner(paths, ["rmux", "send-keys", "-t", pane, "Enter"])

    def apply_runtime_action(self, target: Target, action: str, confirmation: str) -> str:
        require_confirmation(target, confirmation)
        paths = self._paths(target.environment)
        if not self._local_to(paths):
            result = self._run_helper(paths, "runtime-action", target.raw, action, confirmation)
            return redact_log((result.stdout or result.stderr).strip(), 2000)
        result = self._run_as_owner(paths, self.runtime_action_argv(target, action), timeout=60)
        return redact_log((result.stdout or result.stderr or f"{action} complete").strip(), 2000)

    def logs(self, target: Target) -> str:
        paths = self._paths(target.environment)
        if not self._local_to(paths):
            result = self._run_helper(paths, "logs", target.raw)
            return redact_log(result.stdout or "(no logs)")
        if target.kind == "runtime":
            row = self._runtime_row(target)
            session = str(row["rmux_session"] or "")
            if not session:
                raise ControlError("runtime has no RMUX session")
            exact_session = session if session.startswith("=") else f"={session}"
            result = self._run_as_owner(paths, ["rmux", "capture-pane", "-p", "-t", exact_session, "-S", "-250"])
            return redact_log(result.stdout or "(no logs)")
        path = self._hermes_db(target)
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = db.execute(
                "SELECT role,content FROM messages WHERE session_id=? AND COALESCE(active,1)=1 "
                "ORDER BY id DESC LIMIT 40",
                (target.identifier,),
            ).fetchall()
        finally:
            db.close()
        lines = [f"{role}: {str(content or '')}" for role, content in reversed(rows)]
        return redact_log("\n".join(lines) or "(no logs)")

    def _hermes_db(self, target: Target) -> Path:
        if target.kind != "hermes":
            raise ControlError("action requires a Hermes session")
        paths = self._paths(target.environment)
        for _profile, path in paths.hermes_profiles:
            if not path.is_file():
                continue
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                present = db.execute("SELECT 1 FROM sessions WHERE id=?", (target.identifier,)).fetchone()
            finally:
                db.close()
            if present:
                return path
        raise ControlError("Hermes session is unavailable")

    def archive_hermes(self, target: Target) -> None:
        paths = self._paths(target.environment)
        if not self._local_to(paths):
            self._run_helper(paths, "archive-hermes", target.raw)
            return
        path = self._hermes_db(target)
        with sqlite3.connect(path) as database:
            changed = database.execute(
                "UPDATE sessions SET archived=1 WHERE id=? AND COALESCE(archived,0)=0",
                (target.identifier,),
            ).rowcount
        if changed != 1:
            raise ControlError("Hermes session could not be archived")

    def delete_hermes(self, target: Target, confirmation: str) -> None:
        require_confirmation(target, confirmation)
        paths = self._paths(target.environment)
        if not self._local_to(paths):
            self._run_helper(paths, "delete-hermes", target.raw, confirmation)
            return
        path = self._hermes_db(target)
        candidates = [Path(__file__).resolve().parents[3], Path("/opt/agk-terminal/hermes-agent")]
        for candidate in candidates:
            if (candidate / "hermes_state.py").is_file() and str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
        from hermes_state import SessionDB
        database = SessionDB(db_path=path)
        try:
            deleted = database.delete_session(target.identifier, sessions_dir=path.parent / "sessions")
        finally:
            database.close()
        if not deleted:
            raise ControlError("Hermes session could not be deleted")


def _profile_state_dbs(root: Path, exclude: set[str] | None = None) -> list[tuple[str, Path]]:
    rows = [("default", root / "state.db")]
    profiles = root / "profiles"
    excluded = exclude or set()
    try:
        if profiles.is_dir():
            rows.extend((path.name, path / "state.db") for path in sorted(profiles.iterdir()) if path.is_dir() and path.name not in excluded)
    except PermissionError:
        pass
    return rows


def environment_paths(name: str) -> EnvironmentPaths:
    if name == "collective":
        home = Path("/home/agentik")
        return EnvironmentPaths(name, home, home / ".agentik/runtime.db", [("collective", home / ".hermes/profiles/collective/state.db")])
    if name not in {"operator", "agentik", "mission", "private"}:
        raise ControlError("unknown environment")
    home = Path(f"/home/{name}")
    excluded = {"collective"} if name == "agentik" else set()
    return EnvironmentPaths(name, home, home / ".agentik/runtime.db", _profile_state_dbs(home / ".hermes", excluded))


def default_environments() -> dict[str, EnvironmentPaths]:
    result = {"operator": environment_paths("operator")}
    for name in ("agentik", "mission", "private", "collective"):
        home = Path("/home/agentik") if name == "collective" else Path(f"/home/{name}")
        result[name] = EnvironmentPaths(name, home, home / ".agentik/runtime.db", [])
    return result


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["catalog", "prompt", "runtime-action", "logs", "archive-hermes", "delete-hermes"])
    parser.add_argument("environment", choices=sorted(ENVIRONMENTS))
    parser.add_argument("values", nargs="*")
    args = parser.parse_args(argv)
    paths = environment_paths(args.environment)
    controller = StationSessionController(environments={args.environment: paths})
    if args.action == "catalog":
        rows = catalog_for_environment(args.environment, paths.runtime_db, paths.hermes_profiles)
        print(json.dumps([asdict(row) for row in rows], ensure_ascii=False))
    elif args.action == "prompt" and len(args.values) == 1:
        controller.send_prompt(parse_target(args.values[0]), sys.stdin.read())
        print("prompt delivered")
    elif args.action == "runtime-action" and len(args.values) == 3:
        print(controller.apply_runtime_action(parse_target(args.values[0]), args.values[1], args.values[2]))
    elif args.action == "logs" and len(args.values) == 1:
        print(controller.logs(parse_target(args.values[0])))
    elif args.action == "archive-hermes" and len(args.values) == 1:
        controller.archive_hermes(parse_target(args.values[0])); print("archived")
    elif args.action == "delete-hermes" and len(args.values) == 2:
        controller.delete_hermes(parse_target(args.values[0]), args.values[1]); print("deleted")
    else:
        raise ControlError("invalid helper arguments")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
