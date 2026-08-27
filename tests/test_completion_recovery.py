import importlib.util
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "overlay" / "scripts" / "completion_harness.py"
AUDITOR = ROOT / "overlay" / "scripts" / "recovery_auditor.py"


def test_candidate_requirement_extraction_preserves_actionable_lines():
    auditor = load(AUDITOR, "recovery_auditor_extract_test")
    text = """Context only.\n- Add unit tests\n- Verify production after deploy\nAgents must never start backlog work without human approval.\n"""
    rows = auditor.extract_candidate_requirements(text)
    assert "Add unit tests" in rows
    assert "Verify production after deploy" in rows
    assert any("must never start backlog" in row for row in rows)


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prompt_requirement_evidence_gate_and_human_authorization(tmp_path, monkeypatch):
    module = load(HARNESS, "completion_harness_test")
    store = module.CompletionStore(tmp_path / "completion", approval_root=tmp_path / "approvals", oracle_root=tmp_path / "oracle", profile="operator")
    prompt = store.archive_prompt(
        "Build API\n- add tests\n- require CTO approval before prod",
        source="discord",
        session_id="s-1",
        profile="operator",
    )
    mission = store.create_mission("MISS-1", [prompt], client="agk", project="station")
    req_build = store.add_requirement(prompt, "Build the API", mission_id=mission)
    req_test = store.add_requirement(prompt, "Add tests", mission_id=mission)
    req_approval = store.add_requirement(
        prompt, "CTO approval before production", mission_id=mission, human_gate=True
    )

    first = store.completion_gate(mission)
    assert first["classification"] == "INCOMPLETE"
    assert {row["id"] for row in first["unresolved"]} == {req_build, req_test, req_approval}

    artifact = store.add_artifact(mission, req_build, "file", "src/api.py")
    store.add_evidence(mission, req_build, "implementation", "PASS", artifact)
    store.set_requirement_status(req_build, "VERIFIED")
    store.set_requirement_status(req_test, "VERIFIED")
    second = store.completion_gate(mission)
    assert second["classification"] == "INCOMPLETE"
    assert req_test in {row["id"] for row in second["missing_evidence"]}
    assert req_approval in {row["id"] for row in second["human_required"]}

    store.add_evidence(mission, req_test, "test", "PASS", "pytest")
    try:
        store.add_evidence(mission, req_test, "test", "PASS", "")
    except ValueError:
        pass
    else:
        raise AssertionError("empty evidence reference must fail")
    store.db.execute("INSERT INTO authorizations VALUES(?,?,?,?,?,?,?)",("AUTH-FORGED",mission,None,"agent","discord","relaunch:test","now")); store.db.commit()
    assert req_approval in {row["id"] for row in store.completion_gate(mission)["human_required"]}
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module, "_authority_uid", lambda: module.os.getuid())
    store.record_authorization(mission, req_approval, actor="gareth", source="discord", scope="production")
    store.set_requirement_status(req_approval, "VERIFIED")
    store.record_oracle_verdict(mission, actor="completion-oracle", report_sha256="a" * 64,
                                ledger_sha256=store.ledger_digest(mission))
    final = store.completion_gate(mission)
    assert final["classification"] == "COMPLETE"
    assert final["permit_done"] is True
    store.db.execute("UPDATE requirements SET text=? WHERE id=?", ("Changed after approval and Oracle", req_approval)); store.db.commit()
    stale = store.completion_gate(mission)
    assert req_approval in {row["id"] for row in stale["human_required"]}
    assert stale["completion_oracle_passed"] is False
    assert stale["permit_done"] is False
    store.db.execute("UPDATE requirements SET text=? WHERE id=?", ("CTO approval before production", req_approval)); store.db.commit()
    assert store.completion_gate(mission)["permit_done"] is True
    Path(store.get_prompt(prompt)["content_path"]).write_text("tampered archive", encoding="utf-8")
    tampered = store.completion_gate(mission)
    assert tampered["permit_done"] is False
    assert tampered["completion_oracle_passed"] is False
    assert tampered["integrity_error"] == "prompt_archive_integrity_failure"


def test_original_prompt_is_immutable_and_provenance_is_retained(tmp_path):
    module = load(HARNESS, "completion_harness_immutable_test")
    store = module.CompletionStore(tmp_path / "completion")
    prompt = store.archive_prompt("Exact original request", source="discord", session_id="42", profile="mission")
    record = store.get_prompt(prompt)
    assert Path(record["content_path"]).read_text() == "Exact original request"
    requirement = store.add_requirement(prompt, "Do the exact request", provenance="message:42#L1")
    row = store.get_requirement(requirement)
    assert row["prompt_id"] == prompt
    assert row["provenance"] == "message:42#L1"
    assert record["sha256"] == module.sha256_text("Exact original request")
    try:
        store.archive_prompt("Different content", source="discord", session_id="42", profile="mission", source_key=f"mission:discord:42:{record['sha256']}")
    except RuntimeError as error:
        assert "collision" in str(error).lower()
    else:
        raise AssertionError("source-key collision must fail closed")
    Path(record["content_path"]).write_text("tampered")
    try:
        store.prompt_content(prompt)
    except RuntimeError as error:
        assert "integrity" in str(error).lower()
    else:
        raise AssertionError("tampered prompt must fail closed")


def test_false_completion_and_promise_detection(tmp_path):
    module = load(HARNESS, "completion_harness_false_done_test")
    store = module.CompletionStore(tmp_path / "completion")
    prompt = store.archive_prompt("I need A and B", source="cli", session_id="s", profile="operator")
    mission = store.create_mission("MISS-2", [prompt])
    store.add_requirement(prompt, "A", mission_id=mission)
    store.mark_mission_state(mission, "DONE")
    gate = store.completion_gate(mission)
    assert gate["classification"] == "FALSELY_MARKED_DONE"
    promises = module.extract_promises("I will also add tests. Next I will deploy staging.")
    assert len(promises) == 2


def test_auditor_imports_user_prompts_once_and_generates_reports(tmp_path):
    harness = load(HARNESS, "completion_harness_audit_test")
    auditor = load(AUDITOR, "recovery_auditor_test")
    state = tmp_path / "state.db"
    db = sqlite3.connect(state)
    db.executescript(
        "CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, title TEXT, started_at REAL);"
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL, active INTEGER);"
        "INSERT INTO sessions VALUES('s1','discord','Mission one',1000);"
        "INSERT INTO messages VALUES(1,'s1','user','Build the dashboard and add tests',1001,1);"
        "INSERT INTO messages VALUES(2,'s1','assistant','I will also publish the report.',1002,1);"
    )
    db.commit(); db.close()
    root = tmp_path / "completion"
    first = auditor.audit_profile(
        profile="operator", state_db=state, completion_root=root,
        reports_root=tmp_path / "reports", baseline=True,
    )
    second = auditor.audit_profile(
        profile="operator", state_db=state, completion_root=root,
        reports_root=tmp_path / "reports", baseline=False,
    )
    assert first["prompts_imported"] == 1
    assert second["prompts_imported"] == 0
    assert Path(first["audit_report"]).is_file()
    assert Path(first["operator_report"]).is_file()
    text = Path(first["audit_report"]).read_text()
    assert "INCOMPLETE" in text
    assert "Build the dashboard" in Path(first["operator_report"]).read_text()


def test_operator_relaunch_requires_explicit_authorization(tmp_path, monkeypatch):
    module = load(HARNESS, "completion_harness_relaunch_test")
    store = module.CompletionStore(tmp_path / "completion")
    prompt = store.archive_prompt("Recover me", source="discord", session_id="s", profile="operator")
    mission = store.create_mission("MISS-3", [prompt])
    requirement = store.add_requirement(prompt, "Missing work", mission_id=mission)
    finding = store.create_finding(mission, "INCOMPLETE", "P1", [requirement])
    try:
        store.relaunch_finding(finding, authorization={})
    except PermissionError:
        pass
    else:
        raise AssertionError("relaunch without actor must be rejected")
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    package = store.relaunch_finding(finding, authorization={
        "id": "ROOT-AUTH-1", "actor": "gareth", "source": "discord",
        "scope": f"relaunch:{finding}", "timestamp": "2026-08-27T00:00:00+00:00",
        "authority": "root-recovery-router",
    })
    assert package["authorization"]["actor"] == "gareth"
    assert package["requirements"][0]["id"] == requirement
    assert "Recover me" in package["original_prompts"][0]["content"]
