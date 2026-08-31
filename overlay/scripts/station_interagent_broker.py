#!/usr/bin/env python3
"""Root-owned UID-authenticated broker for Station team communication."""
from __future__ import annotations

import json
import logging
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
_INSTALLED_ROUTES_PATH = Path("/etc/agk-terminal/interagent-routes.json")
ROUTES_PATH = Path(
    os.environ.get("AGK_INTERAGENT_ROUTES")
    or (_INSTALLED_ROUTES_PATH if _INSTALLED_ROUTES_PATH.is_file() else Path(__file__).resolve().parents[1] / "config" / "interagent-routes.json")
)
WORK_DISPATCHER = Path("/usr/local/lib/agk-terminal/scripts/station_interagent_work_dispatch.py")
_WORK_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="interagent-thread")
_INFLIGHT_IDS: set[str] = set()
_INFLIGHT_LOCK = threading.Lock()
LOGGER = logging.getLogger("agk.interagent")
_SECRET = re.compile(r"(?i)(?:api[_-]?key|token|password|secret|authorization)\s*[:=]\s*\S{6,}|\bsk-[A-Za-z0-9_-]{8,}")


class BrokerError(ValueError):
    pass


class RouteRegistry:
    def __init__(self, path: Path = ROUTES_PATH):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BrokerError("Station route registry is unavailable") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise BrokerError("Station route registry schema is invalid")
        return value

    def resolve(self, target: str) -> dict:
        key = str(target or "").strip().lower()
        data = self._load()
        if key.startswith("os:"):
            route = (data.get("os") or {}).get(key[3:])
        else:
            route = (data.get("agents") or {}).get(key)
        if not isinstance(route, dict):
            raise BrokerError("unknown Station target")
        owner = str(route.get("owner") or "")
        channel = str(route.get("channel") or "")
        bot = str(route.get("bot") or "")
        home = str(route.get("home") or "")
        if owner not in ENVIRONMENTS or not channel.isdigit() or not bot.isdigit() or not home.startswith("/home/"):
            raise BrokerError("Station route is incomplete")
        return {**route, "id": key, "owner": owner, "channel": channel, "bot": bot, "home": home}

    def targets(self) -> list[str]:
        data = self._load()
        agents = sorted((data.get("agents") or {}).keys())
        systems = [f"os:{key}" for key in sorted((data.get("os") or {}).keys())]
        return agents + systems


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
    def __init__(self, path: Path = DB_PATH, *, routes: RouteRegistry | None = None):
        self.path = Path(path)
        self.routes = routes or RouteRegistry()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS messages(
                id TEXT PRIMARY KEY, source TEXT NOT NULL, target TEXT NOT NULL,
                body TEXT NOT NULL, created_at REAL NOT NULL, acknowledged_at REAL,
                mode TEXT NOT NULL DEFAULT 'note'
            )""")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(messages)")}
            if "mode" not in columns:
                db.execute("ALTER TABLE messages ADD COLUMN mode TEXT NOT NULL DEFAULT 'note'")
        try: self.path.chmod(0o600)
        except OSError: pass

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def send(self, source: str, target: str, body: str, *, mode: str = "delegate") -> dict:
        if source not in ENVIRONMENTS:
            raise BrokerError("unknown Station source")
        target_route = self.routes.resolve(target)
        if "blocked" in str(target_route.get("bot_mode") or ""):
            raise BrokerError("Station target is blocked by an owner-controlled bot credential")
        target = target_route["id"]
        if source == target or source == target_route["owner"] and target == source:
            raise BrokerError("self-directed inter-agent messages are forbidden")
        if mode not in {"note", "delegate"}:
            raise BrokerError("inter-agent send mode must be note or delegate")
        value = validate_message(body)
        record = {"id": uuid.uuid4().hex, "source": source, "target": target, "body": value, "created_at": time.time(), "acknowledged_at": None, "mode": mode}
        with self._connect() as db:
            db.execute(
                "INSERT INTO messages(id,source,target,body,created_at,acknowledged_at,mode) VALUES(?,?,?,?,?,NULL,?)",
                (record["id"], source, target, value, record["created_at"], mode),
            )
        return record

    def inbox(self, target: str, *, requester: str, limit: int = 50) -> list[dict]:
        if target not in ENVIRONMENTS or requester not in ENVIRONMENTS:
            raise BrokerError("unknown Station target")
        if requester != "operator" and requester != target:
            raise BrokerError("cross-Station inbox access denied")
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE target=? AND acknowledged_at IS NULL ORDER BY created_at LIMIT ?",
                (target, max(1, min(int(limit), 100))),
            ).fetchall()
        return [dict(row) for row in rows]

    def pending_delegates(self, limit: int = 100) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM messages WHERE acknowledged_at IS NULL AND mode='delegate' ORDER BY created_at LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def ack(self, message_id: str, *, requester: str) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT target FROM messages WHERE id=?", (str(message_id),)).fetchone()
            if not row:
                return False
            if requester != "operator" and requester != self.routes.resolve(row["target"])["owner"]:
                raise BrokerError("message acknowledgement denied")
            changed = db.execute(
                "UPDATE messages SET acknowledged_at=? WHERE id=? AND acknowledged_at IS NULL",
                (time.time(), str(message_id)),
            ).rowcount
        return changed == 1


def notification_command(target: str, routes: RouteRegistry | None = None) -> tuple[list[str], dict[str, str]]:
    route = (routes or RouteRegistry()).resolve(target)
    owner = route["owner"]
    home = f"/home/{owner}"
    hermes_home = route["home"]
    argv = ["/usr/bin/setpriv", "--reuid", owner, "--regid", owner, "--clear-groups", "/usr/local/bin/hermes", "send", "--to", "discord", "--file", "-", "--quiet"]
    env = {"HOME": home, "HERMES_HOME": hermes_home, "PATH": "/usr/local/bin:/usr/bin:/bin"}
    return argv, env


def notify(record: dict) -> bool:
    argv, env = notification_command(record["target"])
    body = f"[Station inter-agent]\nFrom: {record['source']}\nMessage ID: {record['id']}\n\n{record['body']}"
    result = subprocess.run(argv, input=body, text=True, capture_output=True, env=env, timeout=30, check=False)
    return result.returncode == 0


def dispatch_interagent_work(record: dict, routes: RouteRegistry | None = None) -> dict | None:
    if not WORK_DISPATCHER.is_file(): return None
    registry = routes or RouteRegistry()
    source=registry.resolve(str(record.get("source") or "")); target=registry.resolve(str(record.get("target") or ""))
    if source["id"] == target["id"]: return None
    transport = registry.resolve("operator") if source["bot"] == target["bot"] else source
    owner=transport["owner"]
    argv=["/usr/bin/setpriv","--reuid",owner,"--regid",owner,"--clear-groups","/usr/bin/python3",str(WORK_DISPATCHER)]
    env={"HOME":f"/home/{owner}","HERMES_HOME":transport["home"],"PATH":"/usr/local/bin:/usr/bin:/bin",
         "AGK_TARGET_DISCORD_CHANNEL_ID":target["channel"],"AGK_TARGET_DISCORD_BOT_ID":target["bot"]}
    result=subprocess.run(argv,input=json.dumps(record,ensure_ascii=False),text=True,capture_output=True,env=env,timeout=660,check=False)
    if result.returncode: return None
    try: value=json.loads(result.stdout)
    except (TypeError,ValueError): return None
    return value if isinstance(value,dict) and value.get("success") else None


def finalize_interagent_work(
    store: MessageStore,
    record: dict,
    result: dict | None,
) -> bool:
    """Acknowledge only a handoff accepted in its dedicated work thread."""
    if not isinstance(result, dict):
        return False
    if not result.get("success") or result.get("state") != "accepted":
        return False
    if not result.get("thread_id"):
        return False
    return store.ack(
        str(record.get("id") or ""),
        requester=store.routes.resolve(str(record.get("target") or ""))["owner"],
    )


def process_interagent_work(
    record: dict,
    store: MessageStore,
    *,
    dispatch=dispatch_interagent_work,
    notifier=notify,
) -> bool:
    try:
        result = dispatch(record)
    except Exception:
        result = None
    if finalize_interagent_work(store, record, result):
        return True
    return False


def queue_interagent_work(record: dict, store: MessageStore | None = None) -> bool:
    if record.get("mode", "delegate") != "delegate" or record.get("source")==record.get("target") or not WORK_DISPATCHER.is_file(): return False
    message_id = str(record.get("id") or "")
    if not message_id:
        return False
    with _INFLIGHT_LOCK:
        if message_id in _INFLIGHT_IDS:
            return False
        _INFLIGHT_IDS.add(message_id)
    message_store = store or MessageStore()
    def worker():
        try:
            return process_interagent_work(record, message_store)
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT_IDS.discard(message_id)
    try:
        _WORK_POOL.submit(worker)
    except Exception:
        with _INFLIGHT_LOCK:
            _INFLIGHT_IDS.discard(message_id)
        raise
    return True


def recover_pending_forever(broker, stop_event: threading.Event, *, interval: float = 15.0) -> None:
    """Retry durable unacknowledged handoffs without requiring a broker restart."""
    while not stop_event.wait(max(0.0, float(interval))):
        try:
            broker.recover_pending()
        except Exception as exc:
            LOGGER.error("pending recovery failed type=%s", type(exc).__name__)


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
                mode = str(request.get("mode") or "delegate").strip().lower()
                record = store.send(
                    source,
                    str(request.get("target") or ""),
                    str(request.get("message") or ""),
                    mode=mode,
                )
                queued = queue_interagent_work(record, store=store)
                delivered = False if queued or mode == "note" else notify(record)
                response = {
                    "success": True,
                    "message_id": record["id"],
                    "source": source,
                    "target": record["target"],
                    "mode": mode,
                    "discord_notified": delivered,
                    "work_queued": queued,
                    "thread_created": queued,
                    "operator_work_queued": queued and record["target"] == "operator",
                }
            elif action == "inbox":
                target = str(request.get("target") or source)
                response = {"success": True, "messages": store.inbox(target, requester=source, limit=int(request.get("limit") or 50))}
            elif action == "ack":
                response = {"success": True, "acknowledged": store.ack(str(request.get("message_id") or ""), requester=source)}
            elif action == "list":
                response = {"success": True, "agents": list(ENVIRONMENTS), "targets": store.routes.targets(), "operator_admin": source == "operator"}
            else:
                raise BrokerError("unknown action")
        except Exception as exc:
            LOGGER.error(
                "broker request failed action=%s source=%s type=%s",
                locals().get("action", "unknown"), locals().get("source", "unknown"), type(exc).__name__,
            )
            response = {"success": False, "error": str(exc) if isinstance(exc, BrokerError) else "broker request failed"}
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode())


class Broker(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, socket_path: Path = SOCKET_PATH, db_path: Path = DB_PATH, routes_path: Path = ROUTES_PATH):
        socket_path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        try: socket_path.unlink()
        except FileNotFoundError: pass
        super().__init__(str(socket_path), Handler)
        self.socket_path = socket_path
        self.routes = RouteRegistry(routes_path)
        self.store = MessageStore(db_path, routes=self.routes)
        socket_path.chmod(0o666)

    def recover_pending(self) -> int:
        recovered = 0
        for record in self.store.pending_delegates():
            if queue_interagent_work(record, store=self.store):
                recovered += 1
        return recovered

    def server_close(self):
        super().server_close()
        try: self.socket_path.unlink()
        except FileNotFoundError: pass


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    broker = Broker()
    broker.recover_pending()
    stop_event = threading.Event()
    recovery = threading.Thread(
        target=recover_pending_forever,
        args=(broker, stop_event),
        name="interagent-recovery",
        daemon=True,
    )
    recovery.start()
    try: broker.serve_forever(poll_interval=.25)
    finally:
        stop_event.set()
        recovery.join(timeout=2)
        broker.server_close()
    return 0


if __name__ == "__main__": raise SystemExit(main())
