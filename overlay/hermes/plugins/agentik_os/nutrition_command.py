"""Discord/CLI command bridge for the installed Nutrition OS."""
from __future__ import annotations

import datetime as dt
import fcntl
import importlib.util
import json
import os
import shlex
import uuid
from pathlib import Path

OS_REF = "nutrition-os@1.0.1"
PACKAGE = Path("/opt/agentik/os-registry/packages/nutrition-os/1.0.1")
OPERATOR_ASSIGNMENTS = Path("/etc/agentik/operator-os/assignments.yaml")


def _core():
    path = PACKAGE / "functions/nutrition_ops.py"
    if not path.is_file():
        raise RuntimeError("Nutrition OS package is not installed")
    spec = importlib.util.spec_from_file_location("agk_nutrition_core", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Nutrition OS entrypoint cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _paths() -> tuple[Path, Path]:
    root = Path("/home/operator/.hermes/profiles/nutrition/data/nutrition-os")
    return root / "state.json", root / ".lock"


def _active() -> bool:
    import yaml
    hermes_home = Path(os.environ.get("HERMES_HOME", "")).resolve() if os.environ.get("HERMES_HOME") else None
    dedicated_profile = Path("/home/operator/.hermes/profiles/nutrition").resolve()
    path = OPERATOR_ASSIGNMENTS if (Path.home() == Path("/home/operator") or hermes_home == dedicated_profile) else Path.home() / ".agentik/os-assignments.yaml"
    try:
        records = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("assignments", [])
    except OSError:
        return False
    return any(isinstance(r, dict) and r.get("os") == OS_REF for r in records)


def _load(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("state"), dict):
        raise ValueError("stored Nutrition OS state is invalid")
    return value


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def _summary(record: dict | None) -> str:
    if not record:
        return "NUTRITION OS\nNo active cycle. Use `/nutrition plan <days> <servings> [preferences]`."
    state = record["state"]
    pending = sum(i.get("status") == "pending" for i in state.get("items", []))
    done = sum(i.get("status") == "done" for i in state.get("items", []))
    return "\n".join([
        "NUTRITION OS · ACTIVE",
        f"Cycle: {state['cycle_id']}",
        f"Phase: {state['phase']}",
        f"Revision: {state['revision']}",
        f"Plan: {record['plan']['days']} day(s), {record['plan']['servings']} serving(s)",
        f"Shopping: {done} done, {pending} pending",
        "Scope: kitchen operations only; not medical care.",
    ])


def dispatch(raw_args: str = "") -> str:
    if not _active():
        return f"Nutrition OS is not active in this environment ({OS_REF})."
    try:
        argv = shlex.split(raw_args)
    except ValueError as exc:
        return f"Invalid arguments: {exc}"
    action = argv[0].lower() if argv else "status"
    state_path, lock_path = _paths()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            core = _core()
            record = _load(state_path)
            if action in {"status", "current"}:
                return _summary(record)
            if action == "plan":
                if len(argv) < 3:
                    return "Usage: /nutrition plan <days 1-14> <servings 1-20> [preferences]"
                days, servings = int(argv[1]), int(argv[2])
                if not 1 <= days <= 14 or not 1 <= servings <= 20:
                    raise ValueError("days must be 1-14 and servings 1-20")
                cycle = "nutrition-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
                record = {"state": core.new_state(cycle), "plan": {
                    "days": days, "servings": servings,
                    "preferences": " ".join(argv[3:])[:500],
                }}
                _save(state_path, record)
                return "Plan created.\n" + _summary(record)
            if not record:
                return "No active cycle. Use `/nutrition plan <days> <servings> [preferences]`."
            state = record["state"]
            if action == "shop":
                sub = argv[1].lower() if len(argv) > 1 else "list"
                if sub == "list":
                    items = state.get("items", [])
                    return "SHOPPING LIST\n" + ("\n".join(
                        f"{'✓' if i['status']=='done' else '○'} {i['name']} · {i['id']}" for i in items
                    ) if items else "(empty)")
                if sub == "add":
                    if len(argv) < 3:
                        return "Usage: /nutrition shop add <item> [--expires YYYY-MM-DD]"
                    args = argv[2:]; expires = None
                    if "--expires" in args:
                        pos = args.index("--expires")
                        if pos + 1 >= len(args): raise ValueError("--expires requires YYYY-MM-DD")
                        expires = args[pos + 1]; dt.date.fromisoformat(expires); del args[pos:pos + 2]
                    name = " ".join(args).strip()
                    state = core.add_item(state, name, expires_on=expires, idempotency_key=str(uuid.uuid4()))
                    record["state"] = state; _save(state_path, record)
                    item = state["items"][-1]
                    return f"Shopping item added: {item['name']} · {item['id']}"
                if sub in {"done", "block"} and len(argv) == 3:
                    status = "done" if sub == "done" else "blocked"
                    state = core.set_item_status(state, argv[2], status, str(uuid.uuid4()))
                    record["state"] = state; _save(state_path, record)
                    return f"Shopping item {argv[2]} → {status}."
                return "Usage: /nutrition shop list|add <item> [--expires DATE]|done <id>|block <id>"
            if action in {"prep", "next"}:
                phases = list(core.PHASES); current = state["phase"]
                target = "BATCH" if action == "prep" else phases[(phases.index(current) + 1) % len(phases)]
                if action == "prep" and current != "SHOP":
                    raise ValueError("prep is allowed only from SHOP")
                state = core.transition(state, target, str(uuid.uuid4()))
                record["state"] = state; _save(state_path, record)
                return f"Nutrition cycle advanced: {current} → {target}."
            if action == "audit":
                if len(argv) < 4:
                    return "Usage: /nutrition audit <eaten> <discarded> <remaining> [notes]"
                eaten, discarded, remaining = map(int, argv[1:4])
                if min(eaten, discarded, remaining) < 0: raise ValueError("audit counts must be non-negative")
                state["events"].append({
                    "kind": "nutrition_audit",
                    "payload": {"eaten": eaten, "discarded": discarded, "remaining": remaining,
                                "notes": " ".join(argv[4:])[:500]},
                    "idempotency_key": str(uuid.uuid4()),
                    "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                })
                state["revision"] += 1; record["state"] = state; _save(state_path, record)
                return f"Audit recorded: eaten={eaten}, discarded={discarded}, remaining={remaining}."
            if action == "reset":
                cycle = "nutrition-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
                record["state"] = core.reset_cycle(state, cycle, str(uuid.uuid4()))
                _save(state_path, record); return "Nutrition cycle reset.\n" + _summary(record)
            return "Usage: /nutrition status|plan|shop|prep|next|audit|reset"
        except (ValueError, RuntimeError) as exc:
            return f"Nutrition OS error: {exc}"
