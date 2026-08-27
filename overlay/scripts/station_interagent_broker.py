#!/usr/bin/env python3
"""Root-owned UID-authenticated broker for Station team communication."""
from __future__ import annotations

import json
import os
import pwd
import re
import socket
import socketserver
import sqlite3
import struct
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ENVIRONMENTS = ("operator", "agentik", "mission", "private", "collective")
SOCKET_PATH = Path("/run/agk-station/interagent.sock")
DB_PATH = Path("/var/lib/agk-station/interagent.db")
WORK_DISPATCHER = Path("/usr/local/lib/agk-terminal/scripts/station_interagent_work_dispatch.py")
AGENTS = {
    "operator": {"owner":"operator","home":"/home/operator/.hermes","channel":"1541820137148260432","bot":"1541816910587625492"},
    "agentik": {"owner":"agentik","home":"/home/agentik/.hermes","channel":"1541820106479501322","bot":"1541817976586637382"},
    "mission": {"owner":"mission","home":"/home/mission/.hermes","channel":"1541814383007764570","bot":"1541817162241540126"},
    "private": {"owner":"private","home":"/home/private/.hermes","channel":"1541820077278503072","bot":"1541817649661747351"},
    "collective": {"owner":"mission","home":"/home/mission/.hermes/profiles/collective","channel":"1541847685680603387","bot":"1541131574509314209"},
}
_WORK_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="interagent-thread")
_SECRET = re.compile(r"(?i)(?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S{6,}|\bsk-[A-Za-z0-9_-]{8,}")


class BrokerError(ValueError):
    pass


def uid_map() -> dict[int, str]:
    return {pwd.getpwnam(name).pw_uid: name for name in ("operator", "agentik", "mission", "private")}


def source_for_uid(uid: int, mapping: dict[int, str] | None = None) -> str:
    source = (mapping or uid_map()).get(int(uid))
    if not source:
        raise BrokerError("unknown Station identity")
    return source


def validate_message(text: str) -> str:
    value = str(text or "").strip()
    if not 1 <= len(value) <= 4000:
        raise BrokerError("message must contain 1-4000 characters")
    if _SECRET.search(value):
        raise BrokerError("secret-like content is forbidden in inter-agent messages")
    try:
        from agent.redact import redact_sensitive_text
        if redact_sensitive_text(value, force=True) != value:
            raise BrokerError("secret-like content is forbidden in inter-agent messages")
    except ImportError:
        pass
    return value


class MessageStore:
    def __init__(self, path: Path = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS messages(
            id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL,
            body TEXT NOT NULL, created_at REAL NOT NULL, acknowledged_at REAL
        )""")
        self.db.commit()
        try: self.path.chmod(0o600)
        except OSError: pass

    def send(self, source: str, target: str, body: str) -> dict:
        if source not in ENVIRONMENTS or target not in ENVIRONMENTS:
            raise BrokerError("unknown Station target")
        if source == target:
            raise BrokerError("self-directed inter-agent messages are forbidden")
        value = validate_message(body)
        record = {"id": uuid.uuid4().hex, "source": source, "target": target, "body": value, "created_at": time.time(), "acknowledged_at": None}
        self.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,NULL)", (record["id"], source, target, value, record["created_at"]))
        self.db.commit()
        return record

    def inbox(self, target: str, *, requester: str, limit: int = 50) -> list[dict]:
        if target not in ENVIRONMENTS or requester not in ENVIRONMENTS:
            raise BrokerError("unknown Station target")
        if requester != "operator" and requester != target:
            raise BrokerError("cross-Station inbox access denied")
        rows = self.db.execute(
            "SELECT * FROM messages WHERE target=? AND acknowledged_at IS NULL ORDER BY created_at LIMIT ?",
            (target, max(1, min(int(limit), 100))),
        ).fetchall()
        return [dict(row) for row in rows]

    def ack(self, message_id: str, *, requester: str) -> bool:
        row = self.db.execute("SELECT target FROM messages WHERE id=?", (str(message_id),)).fetchone()
        if not row:
            return False
        if requester != "operator" and requester != row["target"]:
            raise BrokerError("message acknowledgement denied")
        changed = self.db.execute("UPDATE messages SET acknowledged_at=? WHERE id=? AND acknowledged_at IS NULL", (time.time(), str(message_id))).rowcount
        self.db.commit()
        return changed == 1


def notification_command(target: str) -> tuple[list[str], dict[str, str]]:
    if target not in ENVIRONMENTS:
        raise BrokerError("unknown Station target")
    owner = "mission" if target == "collective" else target
    home = f"/home/{owner}"
    hermes_home = f"{home}/.hermes/profiles/collective" if target == "collective" else f"{home}/.hermes"
    argv = ["/usr/bin/setpriv", "--reuid", owner, "--regid", owner, "--clear-groups", "/usr/local/bin/hermes", "send", "--to", "discord", "--file", "-", "--quiet"]
    env = {"HOME": home, "HERMES_HOME": hermes_home, "PATH": "/usr/local/bin:/usr/bin:/bin"}
    return argv, env


def notify(record: dict) -> bool:
    argv, env = notification_command(record["target"])
    body = f"[Station inter-agent]\nFrom: {record['source']}\nMessage ID: {record['id']}\n\n{record['body']}"
    result = subprocess.run(argv, input=body, text=True, capture_output=True, env=env, timeout=30, check=False)
    return result.returncode == 0


def dispatch_interagent_work(record: dict) -> dict | None:
    if not WORK_DISPATCHER.is_file(): return None
    source=AGENTS.get(str(record.get("source") or "")); target=AGENTS.get(str(record.get("target") or ""))
    if not source or not target or source is target: return None
    owner=source["owner"]
    argv=["/usr/bin/setpriv","--reuid",owner,"--regid",owner,"--clear-groups","/usr/bin/python3",str(WORK_DISPATCHER)]
    env={"HOME":f"/home/{owner}","HERMES_HOME":source["home"],"PATH":"/usr/local/bin:/usr/bin:/bin",
         "AGK_TARGET_DISCORD_CHANNEL_ID":target["channel"],"AGK_TARGET_DISCORD_BOT_ID":target["bot"]}
    result=subprocess.run(argv,input=json.dumps(record,ensure_ascii=False),text=True,capture_output=True,env=env,timeout=120,check=False)
    if result.returncode: return None
    try: value=json.loads(result.stdout)
    except (TypeError,ValueError): return None
    return value if isinstance(value,dict) and value.get("success") else None


def queue_interagent_work(record: dict) -> bool:
    if record.get("source")==record.get("target") or not WORK_DISPATCHER.is_file(): return False
    def worker():
        if not dispatch_interagent_work(record): notify(record)
    _WORK_POOL.submit(worker)
    return True


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        peer = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", peer)
        try:
            source = source_for_uid(uid)
            raw = self.rfile.readline(16385)
            if len(raw) > 16384:
                raise BrokerError("request too large")
            request = json.loads(raw)
            if source == "mission" and request.get("station_profile") == "collective":
                source = "collective"
            action = str(request.get("action") or "")
            store: MessageStore = self.server.store
            if action == "send":
                record = store.send(source, str(request.get("target") or ""), str(request.get("message") or ""))
                queued = queue_interagent_work(record)
                delivered = False if queued else notify(record)
                response = {
                    "success": True,
                    "message_id": record["id"],
                    "source": source,
                    "target": record["target"],
                    "discord_notified": delivered,
                    "work_queued": queued,
                    "operator_work_queued": queued and record["target"] == "operator",
                }
            elif action == "inbox":
                target = str(request.get("target") or source)
                response = {"success": True, "messages": store.inbox(target, requester=source, limit=int(request.get("limit") or 50))}
            elif action == "ack":
                response = {"success": True, "acknowledged": store.ack(str(request.get("message_id") or ""), requester=source)}
            elif action == "list":
                response = {"success": True, "agents": list(ENVIRONMENTS), "operator_admin": source == "operator"}
            else:
                raise BrokerError("unknown action")
        except Exception as exc:
            response = {"success": False, "error": str(exc) if isinstance(exc, BrokerError) else "broker request failed"}
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode())


class Broker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: Path = SOCKET_PATH, db_path: Path = DB_PATH):
        socket_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        try: socket_path.unlink()
        except FileNotFoundError: pass
        super().__init__(str(socket_path), Handler)
        self.socket_path = socket_path
        self.store = MessageStore(db_path)
        socket_path.chmod(0o666)

    def server_close(self):
        super().server_close()
        try: self.socket_path.unlink()
        except FileNotFoundError: pass


def main() -> int:
    broker = Broker()
    try: broker.serve_forever(poll_interval=.25)
    finally: broker.server_close()
    return 0


if __name__ == "__main__": raise SystemExit(main())
