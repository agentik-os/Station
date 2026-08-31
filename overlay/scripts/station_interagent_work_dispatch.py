#!/usr/bin/env python3
"""Turn Operator-bound inter-agent messages into durable Discord work threads."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys

import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_PARENT_CHANNEL_ID = "1541820137148260432"
DEFAULT_STORE = Path("/home/operator/.hermes/interagent_work.db")
DEFAULT_CWD = Path("/home/operator/workspace")
_SECRET = re.compile(
    r"(?i)(?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S{6,}|\bsk-[A-Za-z0-9_-]{8,}"
)
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class DispatchError(RuntimeError):
    pass


def validate_record(record: dict) -> dict:
    value = dict(record or {})
    message_id = str(value.get("id") or "")
    source = str(value.get("source") or "")
    body = str(value.get("body") or "").strip()
    if not re.fullmatch(r"[a-f0-9]{32}", message_id):
        raise DispatchError("invalid inter-agent message id")
    if source not in {"operator", "agentik", "mission", "private", "collective"}:
        raise DispatchError("invalid inter-agent source")
    target = str(value.get("target") or "")
    if target not in {"operator", "agentik", "mission", "private", "collective"} and not re.fullmatch(
        r"os:[a-z0-9]+(?:-[a-z0-9]+)*", target
    ):
        raise DispatchError("invalid inter-agent target")
    mode = str(value.get("mode") or "")
    if mode != "delegate":
        raise DispatchError("work threads require explicit cross-agent delegation")
    if source == target:
        raise DispatchError("self-directed bot prompts are forbidden")
    if not 1 <= len(body) <= 4000:
        raise DispatchError("invalid inter-agent message body")
    if _SECRET.search(body):
        raise DispatchError("secret-like content is forbidden in work dispatch")
    return {"id": message_id, "source": source, "target": target, "mode": mode, "body": body}


def thread_title(source: str, body: str) -> str:
    first = re.sub(r"\s+", " ", body.splitlines()[0]).strip()
    first = re.sub(r"^(?:OWNER\s+(?:ESCALATION|STANDARD)|BLOCKER)\s*[—:-]*\s*", "", first, flags=re.I)
    title = f"{source.upper()} · {first or 'Inter-agent work'}"
    return title[:100].rstrip()


def resolve_rmux_session_id(inventory: str, name: str) -> str:
    for line in str(inventory or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[0] == name and parts[1]:
            return parts[1]
    raise DispatchError("AGK work RMUX session not found")


def runtime_ready(snapshot: str) -> bool:
    value = str(snapshot or "")
    return "Welcome to Hermes Agent!" in value and "Type your message" in value


def normalize_instruction(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def is_final_verdict(content: str) -> bool:
    for line in str(content or "").splitlines():
        clean = line.strip().lstrip("*_`#> ")
        if re.match(
            r"(?i)^final\s+verdict\s*[:—-]\s*(?:PASS|PARTIAL|BLOCKED)\b",
            clean,
        ):
            return True
    return False


class WorkStore:
    def __init__(self, path: Path = DEFAULT_STORE):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS work(
                message_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                thread_id TEXT,
                thread_message_id TEXT,
                runtime_name TEXT,
                runtime_id TEXT,
                state TEXT NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS routes(
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(source,target)
            )"""
        )
        self.db.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def get(self, message_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM work WHERE message_id=?", (message_id,)).fetchone()
        return dict(row) if row else None

    def claim(self, record: dict, *, stale_after_seconds: float = 180.0) -> tuple[dict, bool]:
        """Atomically reserve one message ID; reclaim abandoned dispatch leases."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT * FROM work WHERE message_id=?",
                (record["id"],),
            ).fetchone()
            if row is not None:
                state = dict(row)
                stale = (
                    state.get("state") in {"dispatching", "thread-created", "thread-reused", "delivered"}
                    and time.time() - float(state.get("updated_at") or 0) >= stale_after_seconds
                )
                if stale:
                    self.db.execute(
                        "UPDATE work SET updated_at=? WHERE message_id=?",
                        (time.time(), record["id"]),
                    )
                    self.db.commit()
                    return self.get(record["id"]) or {}, True
                self.db.commit()
                return state, False
            now = time.time()
            self.db.execute(
                "INSERT INTO work(message_id,source,state,updated_at) VALUES(?,?,?,?)",
                (record["id"], record["source"], "dispatching", now),
            )
            self.db.commit()
            return self.get(record["id"]) or {}, True
        except Exception:
            self.db.rollback()
            raise

    def route(self, source: str, target: str) -> str | None:
        row = self.db.execute(
            "SELECT thread_id FROM routes WHERE source=? AND target=?",
            (source, target),
        ).fetchone()
        return str(row["thread_id"]) if row else None

    def bind_route(self, source: str, target: str, thread_id: str) -> None:
        self.db.execute(
            """INSERT INTO routes(source,target,thread_id,updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(source,target) DO UPDATE SET
                 thread_id=excluded.thread_id,
                 updated_at=excluded.updated_at""",
            (source, target, thread_id, time.time()),
        )
        self.db.commit()

    def update(self, message_id: str, **values) -> dict:
        allowed = {"thread_id", "thread_message_id", "runtime_name", "runtime_id", "state"}
        clean = {key: value for key, value in values.items() if key in allowed}
        clean["updated_at"] = time.time()
        assignments = ",".join(f"{key}=?" for key in clean)
        self.db.execute(
            f"UPDATE work SET {assignments} WHERE message_id=?",
            (*clean.values(), message_id),
        )
        self.db.commit()
        return self.get(message_id) or {}


class DiscordClient:
    def __init__(self, token: str, *, api_base: str = "https://discord.com/api/v10"):
        if not token:
            raise DispatchError("Operator Discord credential is unavailable")
        self.token = token
        self.api_base = api_base.rstrip("/")

    def _request(self, method: str, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self.api_base + path,
            data=json.dumps(payload).encode(),
            method=method,
            headers={
                "Authorization": f"Bot {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "AGK-Station-Interagent/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                value = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise DispatchError(f"Discord work-thread request failed: HTTP {exc.code}") from None
        except (OSError, ValueError) as exc:
            raise DispatchError(f"Discord work-thread request failed: {type(exc).__name__}") from None
        if not isinstance(value, dict) or not value.get("id"):
            raise DispatchError("Discord work-thread response is invalid")
        return value

    def create_thread(self, parent_channel_id: str, name: str) -> str:
        value = self._request(
            "POST",
            f"/channels/{int(parent_channel_id)}/threads",
            {"name": name, "type": 11, "auto_archive_duration": 60},
        )
        return str(value["id"])

    def add_thread_member(self, thread_id: str, target_bot_id: str) -> None:
        request = urllib.request.Request(
            self.api_base + f"/channels/{int(thread_id)}/thread-members/{int(target_bot_id)}",
            data=b"",
            method="PUT",
            headers={
                "Authorization": f"Bot {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "AGK-Station-Interagent/1.0",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status not in {200, 201, 204}:
                    raise DispatchError("Discord target thread membership failed")
        except urllib.error.HTTPError as exc:
            raise DispatchError(f"Discord target thread membership failed: HTTP {exc.code}") from None
        except OSError as exc:
            raise DispatchError(f"Discord target thread membership failed: {type(exc).__name__}") from None

    def reuse_thread(self, thread_id: str) -> str:
        value = self._request(
            "PATCH",
            f"/channels/{int(thread_id)}",
            {"archived":False,"locked":False,"auto_archive_duration":60},
        )
        return str(value["id"])

    def post_handoff(self, thread_id: str, content: str, target_bot_id: str) -> str:
        value = self._request(
            "POST",
            f"/channels/{int(thread_id)}/messages",
            {"content": content[:1900], "allowed_mentions": {"users": [str(target_bot_id)], "parse": []}},
        )
        return str(value["id"])

    def wait_for_bot_reply(self, thread_id: str, target_bot_id: str, after_message_id: str) -> str:
        deadline=time.time()+600
        while time.time()<deadline:
            request=urllib.request.Request(self.api_base+f"/channels/{int(thread_id)}/messages?limit=20",headers={"Authorization":f"Bot {self.token}","User-Agent":"AGK-Station-Interagent/1.0"})
            try:
                with urllib.request.urlopen(request,timeout=10) as response: rows=json.loads(response.read() or b"[]")
            except (OSError,ValueError): rows=[]
            for row in rows if isinstance(rows,list) else []:
                if (
                    str(row.get("id") or "") > str(after_message_id)
                    and str((row.get("author") or {}).get("id") or "") == str(target_bot_id)
                    and is_final_verdict(str(row.get("content") or ""))
                ):
                    return str(row["id"])
            time.sleep(1)
        raise DispatchError("target bot did not accept the thread handoff")


class RuntimeLauncher:
    def __init__(self, cwd: Path = DEFAULT_CWD):
        self.cwd = Path(cwd).resolve()

    def launch(self, name: str, instruction: str) -> str:
        if not _NAME.fullmatch(name):
            raise DispatchError("invalid AGK runtime name")
        create = subprocess.run(
            [
                "/usr/local/bin/agk", "new", "hermes", name,
                "--cwd", str(self.cwd), "--mission", name,
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        existed = create.returncode != 0 and "already" in (create.stderr + create.stdout).lower()
        if create.returncode != 0 and not existed:
            raise DispatchError("AGK work runtime creation failed")
        match = re.search(r"\bRT-[A-Z0-9]+\b", create.stdout)
        runtime_id = match.group(0) if match else "existing"

        pane = ""
        snapshot = ""
        for _ in range(120):
            sessions = subprocess.run(
                ["rmux", "list-sessions", "-F", "#{session_name}\t#{session_id}"],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            try:
                session_id = resolve_rmux_session_id(sessions.stdout, name)
            except DispatchError:
                time.sleep(0.25)
                continue
            listed = subprocess.run(
                ["rmux", "list-panes", "-t", session_id, "-F", "#{pane_id}"],
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            pane = next((line.strip() for line in listed.stdout.splitlines() if line.strip()), "")
            if pane:
                captured = subprocess.run(
                    ["rmux", "capture-pane", "-p", "-t", pane, "-S", "-200"],
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                snapshot = captured.stdout
                if runtime_ready(snapshot):
                    break
                if existed and any(marker in snapshot for marker in ("Initializing agent...", "┌─ Reasoning", "Working (")):
                    return runtime_id
            time.sleep(0.25)
        if not pane or not runtime_ready(snapshot):
            raise DispatchError("AGK work runtime did not reach the Hermes prompt")

        normalized = normalize_instruction(instruction)
        sent = subprocess.run(
            ["rmux", "send-keys", "-t", pane, "-l", normalized],
            timeout=5,
            check=False,
        )
        time.sleep(0.2)
        entered = subprocess.run(["rmux", "send-keys", "-t", pane, "Enter"], timeout=5, check=False)
        if sent.returncode or entered.returncode:
            raise DispatchError("AGK work instruction delivery failed")
        return runtime_id


class Dispatcher:
    def __init__(self, *, store: WorkStore, discord, parent_channel_id: str, target_bot_id: str):
        self.store = store
        self.discord = discord
        self.parent_channel_id = str(parent_channel_id)
        self.target_bot_id = str(target_bot_id)

    def dispatch(self, raw_record: dict) -> dict:
        record = validate_record(raw_record)
        state, claimed = self.store.claim(record)
        if not claimed:
            if state.get("state") == "accepted" and state.get("thread_id") and state.get("thread_message_id"):
                return state
            raise DispatchError("inter-agent handoff is already being dispatched")
        thread_id = state.get("thread_id")
        if not thread_id:
            pair_thread = self.store.route(record["source"], record["target"])
            if pair_thread:
                thread_id = self.discord.reuse_thread(pair_thread)
                next_state = "thread-reused"
            else:
                thread_id = self.discord.create_thread(
                    self.parent_channel_id,
                    f"{record['source'].upper()} → {record['target'].upper()} · handoffs",
                )
                self.store.bind_route(record["source"], record["target"], thread_id)
                next_state = "thread-created"
            state = self.store.update(
                record["id"],
                thread_id=thread_id,
                state=next_state,
            )
        thread_message_id = state.get("thread_message_id")
        if not thread_message_id:
            self.discord.add_thread_member(thread_id, self.target_bot_id)
            content="\n".join([
                f"<@{self.target_bot_id}>",
                f"**Inter-agent request · {record['source'].upper()} → {record['target'].upper()}**",
                f"Message ID: `{record['id']}`",
                "This is a dedicated work thread. Do not merge it into another active conversation.",
                "",
                record["body"],
                "",
                "Execute the requested work in this thread using the necessary tools. Never synthesize tool output or claim an action you did not execute.",
                "Post progress after substantive steps. Finish with a separate final line beginning exactly `Final verdict: PASS`, `Final verdict: PARTIAL`, or `Final verdict: BLOCKED`.",
            ])
            thread_message_id=self.discord.post_handoff(thread_id,content,self.target_bot_id)
            state=self.store.update(record["id"],thread_message_id=thread_message_id,state="delivered")
        reply_id=self.discord.wait_for_bot_reply(thread_id,self.target_bot_id,thread_message_id)
        return self.store.update(record["id"],runtime_id=reply_id,state="accepted")


def _read_env_value(path: Path, key: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return ""


def main() -> int:
    try:
        record = json.loads(sys.stdin.read(16384))
        hermes_home=Path(os.environ.get("HERMES_HOME") or Path.home()/".hermes")
        token = _read_env_value(hermes_home/".env", "DISCORD_BOT_TOKEN")
        dispatcher = Dispatcher(
            store=WorkStore(hermes_home/"interagent_work.db"),
            discord=DiscordClient(token),
            parent_channel_id=os.environ["AGK_TARGET_DISCORD_CHANNEL_ID"],
            target_bot_id=os.environ["AGK_TARGET_DISCORD_BOT_ID"],
        )
        result = dispatcher.dispatch(record)
        print(json.dumps({
            "success": True,
            "message_id": result["message_id"],
            "thread_id": result["thread_id"],
            "handoff_message_id": result["thread_message_id"],
            "acceptance_message_id": result["runtime_id"],
            "state": result["state"],
        }))
        return 0
    except Exception as exc:
        payload = {"success": False, "error_class": type(exc).__name__}
        if isinstance(exc, DispatchError):
            payload["error"] = str(exc)
        print(json.dumps(payload))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
