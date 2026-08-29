import importlib.util
import json
import os
import shutil
import sqlite3
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "overlay" / "scripts" / "fleet_snapshot.py"


def load_module():
    spec = importlib.util.spec_from_file_location("fleet_snapshot_test", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_fixture(root: Path, org: str) -> dict[str, Path]:
    home = root / "home" / org
    hermes = home / ".hermes"
    board = hermes / "kanban" / "boards" / f"{org}-station"
    board.mkdir(parents=True)
    (hermes / "kanban" / "current").write_text(f"{org}-station\n")
    (board / "board.json").write_text(json.dumps({
        "slug": f"{org}-station", "name": f"{org.title()} Station",
        "description": "Station board", "icon": "◆", "color": "#7170ff",
        "archived": False,
    }))
    db = sqlite3.connect(board / "kanban.db")
    db.execute("CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, status TEXT, priority INTEGER, created_at INTEGER, started_at INTEGER, completed_at INTEGER, session_id TEXT, project_id TEXT, block_kind TEXT)")
    db.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
        "t_123", "Ship the dashboard", "default", "running", 3, 100, 110, None,
        "session-1", "project-1", None,
    ))
    db.commit(); db.close()

    state = sqlite3.connect(hermes / "state.db")
    state.execute("CREATE TABLE sessions (id TEXT, title TEXT, display_name TEXT, parent_session_id TEXT, source TEXT, model TEXT, started_at REAL, ended_at REAL, last_activity_at REAL, message_count INTEGER, tool_call_count INTEGER, archived INTEGER, hidden INTEGER, profile_name TEXT)")
    state.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        "session-1", "Dashboard build", "AGK / #operator", None, "cli", "gpt-5.6-sol", 90, None, 120,
        4, 2, 0, 0, "default",
    ))
    state.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        "session-child", None, None, "session-1", "subagent", "gpt-5.6-sol", 100, 130, 130,
        2, 1, 0, 0, None,
    ))
    state.commit(); state.close()

    agentik = home / ".agentik"
    agentik.mkdir()
    runtime = sqlite3.connect(agentik / "runtime.db")
    runtime.execute("CREATE TABLE runtime_sessions (id TEXT, name TEXT, type TEXT, environment TEXT, status TEXT, last_activity REAL, archived_at REAL, hermes_profile TEXT)")
    runtime.execute("INSERT INTO runtime_sessions VALUES (?,?,?,?,?,?,?,?)", (
        "r1", "builder", "hermes", org, "working", 121, None, "default",
    ))
    runtime.commit(); runtime.close()
    (agentik / "os-assignments.yaml").write_text(yaml.safe_dump({
        "schema_version": 1,
        "assignments": [{"os": "missing-os@0.1.0", "scope": "environment", "target": org}],
    }))

    agent_dir = hermes / "agents" / "builder"
    agent_dir.mkdir(parents=True)
    (agent_dir / "prompt.md").write_text("private prompt content")
    (agent_dir / "agent.yaml").write_text(yaml.safe_dump({
        "id": "builder", "name": "Builder", "version": "1.0.0",
        "description": "Builds verified artifacts", "scope": [org],
        "runtime": "hermes", "profile": "reviewer", "prompt": "prompt.md",
    }))
    profile_dir = hermes / "profiles" / "reviewer"
    profile_dir.mkdir(parents=True)
    (profile_dir / "config.yaml").write_text("model:\n  default: gpt-test\n")
    standalone = hermes / "profiles" / "agk-architect"
    standalone.mkdir(parents=True)
    (standalone / "config.yaml").write_text("model:\n  default: gpt-test\n")
    (standalone / "profile.yaml").write_text(yaml.safe_dump({
        "display_name": "AGK Architect",
        "description": "Designs AGK systems",
    }))
    (standalone / ".env").write_text(
        "DISCORD_BOT_TOKEN=test-token\n"
        "DISCORD_ALLOWED_USERS=1441423462492016821\n"
        "DISCORD_ALLOWED_CHANNELS=1542137541572956193\n"
        "DISCORD_FREE_RESPONSE_CHANNELS=1542137541572956193\n"
        "DISCORD_HOME_CHANNEL=1542137541572956193\n"
    )
    process_id = os.getpid()
    process_stat = Path(f"/proc/{process_id}/stat").read_text()
    process_start_time = int(process_stat[process_stat.rfind(")") + 2:].split()[19])
    (standalone / "gateway_state.json").write_text(json.dumps({
        "pid": process_id,
        "start_time": process_start_time,
        "platforms": {"discord": {"state": "connected", "writer_pid": process_id, "writer_start_time": process_start_time}},
    }))
    (standalone / "discord-runtime-receipt.json").write_text(json.dumps({
        "readiness": True,
        "application_id": "1542135948475637861",
        "home_channel": "1542137541572956193",
        "e2e": {"exact_reply": True},
    }))
    unit_dir = home / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "hermes-gateway-agk-architect.service").write_text("[Service]\n")
    return {"home": home, "hermes": hermes}


def test_snapshot_collects_only_bounded_operational_metadata(tmp_path):
    module = load_module()
    fixture = make_fixture(tmp_path, "operator")
    registry = tmp_path / "registry"
    package = registry / "packages" / "ops-os" / "1.0.0"
    package.mkdir(parents=True)
    (registry / "packages" / "agk-architect" / "1.0.0").mkdir(parents=True)
    (registry / "state").mkdir()
    (registry / "state" / "index.json").write_text(json.dumps({"packages": [{
        "id": "ops-os", "name": "Ops OS", "version": "1.0.0",
        "description": "Operations", "scope": ["operator"],
        "agents": ["builder"], "skills": [], "workflows": [], "tools": [],
        "commands": [], "knowledge": [], "evals": [], "dependencies": [],
        "capabilities": [],
    }, {
        "id": "agk-architect", "name": "AGK Architect OS", "version": "1.0.0",
        "description": "Architecture", "scope": ["operator"],
        "agents": ["agk-architect"], "skills": [], "workflows": [], "tools": [],
        "commands": [], "knowledge": [], "evals": [], "dependencies": [],
        "capabilities": [],
    }]}))

    snapshot = module.collect_snapshot(
        homes={"operator": fixture["home"]}, registry_root=registry, now=200,
    )
    station = snapshot["organisations"]["operator"]
    assert station["kanban"]["counts"]["running"] == 1
    assert station["kanban"]["tasks"][0]["title"] == "Ship the dashboard"
    sessions = {session["id"]: session for session in station["sessions"]}
    assert sessions["session-1"]["title"] == "Dashboard build"
    assert sessions["session-child"]["title"] == "Subagent · Dashboard build"
    assert sessions["session-child"]["profile"] == "default"
    agents = {agent["id"]: agent for agent in station["agents"]}
    assert set(agents) == {"agk-architect", "builder"}
    architect = agents["agk-architect"]
    assert architect["name"] == "AGK Architect"
    assert architect["description"] == "Designs AGK systems"
    assert architect["discord"]["status"] == "connected"
    assert architect["discord"]["service_installed"] is True
    assert architect["discord"]["token_configured"] is True
    assert architect["discord"]["owner_locked"] is True
    assert architect["discord"]["channel_id"] == "1542137541572956193"
    assert architect["discord"]["channel_access"] is True
    assert architect["discord"]["os_access"] is True
    assert architect["discord"]["ready"] is True
    shutil.rmtree(registry / "packages" / "agk-architect")
    without_package = module.collect_snapshot(
        homes={"operator": fixture["home"]}, registry_root=registry, now=201,
    )
    unproven = next(item for item in without_package["organisations"]["operator"]["agents"] if item["id"] == "agk-architect")
    assert unproven["discord"]["e2e_verified"] is True
    assert unproven["discord"]["os_access"] is False
    assert unproven["discord"]["ready"] is False
    assert agents["builder"]["discord"]["status"] == "owner_required"
    (fixture["hermes"] / "profiles" / "agk-architect" / "gateway_state.json").write_text(json.dumps({
        "pid": 99999999,
        "platforms": {"discord": {"state": "connected", "writer_pid": 99999999}},
    }))
    stale = module._discord_profile_state(fixture["hermes"], "agk-architect")
    assert stale["gateway_connected"] is False
    assert stale["status"] == "configured"
    operating_systems = {item["id"]: item for item in station["os"]}
    assert operating_systems["missing-os"]["installed"] is False
    assert operating_systems["ops-os"]["installed"] is True
    assert station["runtimes"][0]["status"] == "working"
    encoded = json.dumps(snapshot)
    assert "private prompt content" not in encoded
    assert str(tmp_path) not in encoded


def test_routing_request_locks_owner_and_channel_atomically(tmp_path):
    module = load_module()
    fixture = make_fixture(tmp_path, "private")
    requests = tmp_path / "requests"
    requests.mkdir()
    (requests / "one.json").write_text(json.dumps({
        "schema": "agk.agent-discord-routing.v1",
        "organisation": "private",
        "profile": "agk-architect",
        "application_id": "1542135948475637861",
        "channel_id": "1542137541572956193",
        "owner_id": "1441423462492016821",
    }))
    applied, failed = module.process_routing_requests(
        requests, {"private": fixture["home"], "operator": Path.home()}, "1441423462492016821",
    )
    assert (applied, failed) == (1, 0)
    env = (fixture["hermes"] / "profiles" / "agk-architect" / ".env").read_text()
    assert env.count("DISCORD_ALLOWED_USERS=1441423462492016821") == 1
    assert env.count("DISCORD_ALLOWED_CHANNELS=1542137541572956193") == 1
    assert env.count("DISCORD_FREE_RESPONSE_CHANNELS=1542137541572956193") == 1
    assert env.count("DISCORD_HOME_CHANNEL=1542137541572956193") == 1
    assert not list(requests.glob("*.json"))


def test_routing_request_rolls_back_env_and_config_when_receipt_write_fails(tmp_path, monkeypatch):
    module = load_module()
    fixture = make_fixture(tmp_path, "private")
    profile = fixture["hermes"] / "profiles" / "agk-architect"
    env_before = (profile / ".env").read_bytes()
    config_before = (profile / "config.yaml").read_bytes()
    requests = tmp_path / "requests"
    requests.mkdir()
    (requests / "two.json").write_text(json.dumps({
        "schema": "agk.agent-discord-routing.v1", "organisation": "private",
        "profile": "agk-architect", "application_id": "1542135948475637861",
        "channel_id": "1542137541572956193",
        "owner_id": "1441423462492016821",
    }))
    real_write = module._write_at_atomic

    def fail_receipt(directory_fd, name, content, mode, uid, gid):
        if name == "discord-routing-receipt.json" and "agk.agent-discord-routing-receipt.v1" in content:
            raise OSError("injected receipt failure")
        return real_write(directory_fd, name, content, mode, uid, gid)

    monkeypatch.setattr(module, "_write_at_atomic", fail_receipt)
    applied, failed = module.process_routing_requests(
        requests, {"private": fixture["home"], "operator": Path.home()}, "1441423462492016821",
    )
    assert (applied, failed) == (0, 1)
    assert (profile / ".env").read_bytes() == env_before
    assert (profile / "config.yaml").read_bytes() == config_before
    assert not (profile / "discord-routing-receipt.json").exists()


def test_secure_input_request_launches_isolated_profile_session(tmp_path, monkeypatch):
    module = load_module()
    fixture = make_fixture(tmp_path, "private")
    requests = tmp_path / "requests"
    status = tmp_path / "status"
    requests.mkdir()
    (requests / "secure.json").write_text(json.dumps({
        "schema": "agk.agent-discord-secure-input.v1", "organisation": "private",
        "profile": "agk-architect", "application_id": "1542135948475637861",
        "channel_id": "1542137541572956193", "guild_id": "1541131439599386644",
        "owner_id": "1441423462492016821", "expected_os_id": "agk-architect",
        "expected_os_version": "1.0.0",
    }))
    calls = []
    monkeypatch.setattr(module.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    applied, failed = module.process_routing_requests(
        requests, {"private": fixture["home"], "operator": Path.home()},
        "1441423462492016821", status,
    )
    assert (applied, failed) == (1, 0)
    command = calls[0][0]
    assert "/usr/bin/systemd-run" == command[0]
    assert "--uid=root" in command
    rendered = " ".join(command)
    assert "/usr/sbin/runuser" in rendered and '"private"' in rendered
    assert "--expected-application" in rendered
    assert "1542135948475637861" in rendered
    assert "--home-channel" in rendered
    assert "1542137541572956193" in rendered
    assert "--profile-id" in rendered
    assert "--expected-os-id" in rendered
    assert "--expected-os-version" in rendered
    assert not list(requests.glob("*.json"))


def test_schema_tolerant_collectors_do_not_require_optional_order_columns(tmp_path):
    module = load_module()
    home = tmp_path / "home" / "private"
    hermes = home / ".hermes"
    board = hermes / "kanban" / "boards" / "default"
    board.mkdir(parents=True)
    tasks_db = sqlite3.connect(hermes / "kanban.db")
    tasks_db.execute("CREATE TABLE tasks (id TEXT, title TEXT, status TEXT)")
    tasks_db.execute("INSERT INTO tasks VALUES ('legacy-task', 'Legacy task', 'running')")
    tasks_db.commit(); tasks_db.close()

    runtime_root = home / ".agentik"
    runtime_root.mkdir()
    runtime_db = sqlite3.connect(runtime_root / "runtime.db")
    runtime_db.execute(
        "CREATE TABLE runtime_sessions (id TEXT, name TEXT, type TEXT, environment TEXT, status TEXT)"
    )
    runtime_db.execute(
        "INSERT INTO runtime_sessions VALUES ('legacy-runtime', 'Legacy', 'hermes', 'private', 'working')"
    )
    runtime_db.commit(); runtime_db.close()

    _, _, tasks = module._boards(hermes)
    runtimes = module._runtimes(home, "private")
    assert [task["id"] for task in tasks] == ["legacy-task"]
    assert [runtime["id"] for runtime in runtimes] == ["legacy-runtime"]


def test_atomic_write_uses_public_read_only_snapshot(tmp_path):
    module = load_module()
    target = tmp_path / "fleet-snapshot.json"
    module.atomic_write(target, {"schema": "agk.fleet.v1"})
    assert json.loads(target.read_text())["schema"] == "agk.fleet.v1"
    assert target.stat().st_mode & 0o777 == 0o640
