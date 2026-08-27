import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MODULE=ROOT/"overlay/hermes/plugins/platforms/discord/agk_recovery_ui.py"
HARNESS=ROOT/"overlay/scripts/completion_harness.py"


def load(path,name):
    spec=importlib.util.spec_from_file_location(name,path); assert spec and spec.loader
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def test_recap_is_registered_and_survives_ui_only_pruning():
    ui=MODULE.read_text(); adapter=(ROOT/"overlay/hermes/plugins/platforms/discord/adapter.py").read_text()
    assert '@tree.command(name="recap"' in ui
    assert 'if result.get("idempotent")' in ui
    assert '"chat","--resume",str(session_id)' in ui
    assert "adapter.handle_message(event)" not in ui
    assert "fcntl.flock" in (ROOT/"overlay/scripts/recovery_router.py").read_text()
    assert '"station-recovery", "recap", "panel"' in adapter


def test_resume_helper_targets_exact_bound_session(tmp_path,monkeypatch):
    import asyncio
    ui=load(MODULE,"recap_resume_test"); captured={}
    class Process:
        returncode=0
        async def communicate(self,data): captured["stdin"]=data
    async def fake_exec(*args,**kwargs): captured["args"]=args; return Process()
    monkeypatch.setattr(ui.asyncio,"create_subprocess_exec",fake_exec)
    result=asyncio.run(ui._resume_bound_session("session-original","continue graph"))
    assert result==0 and "session-original" in captured["args"] and captured["stdin"]==b"continue graph"


def test_recap_resolves_current_discord_session_and_full_graph(tmp_path):
    ui=load(MODULE,"recap_ui_test"); harness=load(HARNESS,"recap_harness_test")
    state=tmp_path/"state.db"; db=sqlite3.connect(state)
    db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,chat_id TEXT,thread_id TEXT,title TEXT,last_activity_at REAL)")
    db.execute("INSERT INTO sessions VALUES('session-1','discord','123','123','Current mission',42)"); db.commit(); db.close()
    completion=tmp_path/"completion"; store=harness.CompletionStore(completion,approval_root=tmp_path/"approvals",oracle_root=tmp_path/"oracle",profile="operator")
    prompt=store.archive_prompt("Build A and test B",source="discord",session_id="session-1",profile="operator")
    mission=store.create_mission("HIST-"+harness.sha256_text("session-1")[:12],[prompt]); done=store.add_requirement(prompt,"Build A",mission_id=mission); missing=store.add_requirement(prompt,"Test B",mission_id=mission)
    store.add_evidence(mission,done,"pytest","PASS","test-report"); store.set_requirement_status(done,"VERIFIED"); store.close()
    controller=ui.RecapController(state_db=state,completion_root=completion,harness_path=HARNESS,auditor_path=tmp_path/"missing-auditor.py",require_fresh_audit=False,approval_root=tmp_path/"approvals",oracle_root=tmp_path/"oracle")
    recap=controller.build("123")
    assert recap["session_id"]=="session-1" and recap["mission_id"]==mission
    assert recap["requirements_total"]==2 and recap["verified"]==1 and recap["unfinished"]==1
    assert recap["permit_done"] is False and recap["systems"]["requirement_graph"] is True
    assert recap["unresolved"][0]["id"]==missing


def test_recap_ensure_finding_is_idempotent(tmp_path):
    ui=load(MODULE,"recap_finding_test"); harness=load(HARNESS,"recap_finding_harness")
    state=tmp_path/"state.db"; db=sqlite3.connect(state); db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,chat_id TEXT,thread_id TEXT,title TEXT,last_activity_at REAL)"); db.execute("INSERT INTO sessions VALUES('s','discord','9',NULL,'X',1)"); db.commit(); db.close()
    completion=tmp_path/"completion"; store=harness.CompletionStore(completion,profile="operator"); p=store.archive_prompt("Fix X",source="discord",session_id="s",profile="operator"); m=store.create_mission("HIST-"+harness.sha256_text("s")[:12],[p]); store.add_requirement(p,"Fix X",mission_id=m); store.close()
    controller=ui.RecapController(state_db=state,completion_root=completion,harness_path=HARNESS,auditor_path=tmp_path/"missing-auditor.py",require_fresh_audit=False)
    first=controller.ensure_finding("9"); second=controller.ensure_finding("9")
    assert first==second and first.startswith("FIND-")


def test_recap_finding_contains_every_unresolved_requirement(tmp_path):
    ui=load(MODULE,"recap_all_test"); harness=load(HARNESS,"recap_all_harness")
    state=tmp_path/"state.db"; db=sqlite3.connect(state); db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,chat_id TEXT,thread_id TEXT,title TEXT,last_activity_at REAL)"); db.execute("INSERT INTO sessions VALUES('all','discord','88',NULL,'All',1)"); db.commit(); db.close()
    completion=tmp_path/"completion"; store=harness.CompletionStore(completion,profile="operator"); p=store.archive_prompt("Many",source="discord",session_id="all",profile="operator"); mission=store.create_mission("HIST-"+harness.sha256_text("all")[:12],[p])
    for index in range(30): store.add_requirement(p,f"Requirement {index}",mission_id=mission)
    store.close(); controller=ui.RecapController(state_db=state,completion_root=completion,harness_path=HARNESS,auditor_path=tmp_path/"none",require_fresh_audit=False)
    recap=controller.build("88"); finding=controller.ensure_finding_for_recap(recap)
    store=harness.CompletionStore(completion,profile="operator"); row=store.db.execute("SELECT requirement_ids_json FROM findings WHERE id=?",(finding,)).fetchone(); store.close()
    assert recap["unfinished"]==30 and len(json.loads(row[0]))==30


def test_failed_audit_is_visible_and_blocks_relaunch(tmp_path):
    ui=load(MODULE,"recap_degraded_test"); harness=load(HARNESS,"recap_degraded_harness")
    state=tmp_path/"state.db"; db=sqlite3.connect(state); db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,chat_id TEXT,thread_id TEXT,title TEXT,last_activity_at REAL)"); db.execute("INSERT INTO sessions VALUES('degraded','discord','66',NULL,'D',1)"); db.commit(); db.close()
    completion=tmp_path/"completion"; store=harness.CompletionStore(completion,profile="operator"); p=store.archive_prompt("Fix",source="discord",session_id="degraded",profile="operator"); mission=store.create_mission("HIST-"+harness.sha256_text("degraded")[:12],[p]); store.add_requirement(p,"Fix",mission_id=mission); store.close()
    broken=tmp_path/"broken.py"; broken.write_text("raise SystemExit(1)\n")
    controller=ui.RecapController(state_db=state,completion_root=completion,harness_path=HARNESS,auditor_path=broken,require_fresh_audit=True)
    recap=controller.build("66"); assert recap["audit_fresh"] is False and recap["classification"]=="AUDIT_DEGRADED" and recap["permit_done"] is False
    try: controller.ensure_finding_for_recap(recap)
    except RuntimeError as error: assert "fresh" in str(error)
    else: raise AssertionError("degraded audit must block relaunch")


def test_recap_confirmation_binds_session_and_blocks_duplicate_relaunch(tmp_path):
    ui=load(MODULE,"recap_bound_test"); harness=load(HARNESS,"recap_bound_harness")
    state=tmp_path/"state.db"; db=sqlite3.connect(state); db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,chat_id TEXT,thread_id TEXT,title TEXT,last_activity_at REAL)"); db.execute("INSERT INTO sessions VALUES('original','discord','77',NULL,'Original',1)"); db.commit(); db.close()
    completion=tmp_path/"completion"; store=harness.CompletionStore(completion,profile="operator"); p=store.archive_prompt("Fix original",source="discord",session_id="original",profile="operator"); mission=store.create_mission("HIST-"+harness.sha256_text("original")[:12],[p]); store.add_requirement(p,"Fix original",mission_id=mission); store.close()
    controller=ui.RecapController(state_db=state,completion_root=completion,harness_path=HARNESS,auditor_path=tmp_path/"none",require_fresh_audit=False)
    recap=controller.build("77"); finding=controller.ensure_finding_for_recap(recap)
    store=harness.CompletionStore(completion,profile="operator"); store.db.execute("UPDATE findings SET human_decision='RELAUNCH' WHERE id=?",(finding,)); store.db.commit(); ledger=store.ledger_digest(mission); store.close()
    (completion/"relaunch").mkdir(exist_ok=True); (completion/"relaunch"/f"{finding}.json").write_text(json.dumps({"mission_id":mission,"ledger_sha256":ledger}))
    store=harness.CompletionStore(completion,profile="operator"); store.create_finding(mission,"INCOMPLETE","P2",[]); store.close()
    db=sqlite3.connect(state); db.execute("INSERT INTO sessions VALUES('new-session','discord','77',NULL,'New',2)"); db.commit(); db.close()
    assert recap["session_id"]=="original"
    try: controller.ensure_finding_for_recap(recap)
    except RuntimeError as error: assert "already relaunched" in str(error)
    else: raise AssertionError("same ledger must not produce duplicate recovery")
