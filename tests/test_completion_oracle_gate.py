import importlib.util
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MODULE=ROOT/"overlay/scripts/completion_oracle_gate.py"
HARNESS=ROOT/"overlay/scripts/completion_harness.py"


def load(path,name):
 spec=importlib.util.spec_from_file_location(name,path); assert spec and spec.loader
 module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def test_oracle_gate_requires_root_owned_validation_and_complete_report(tmp_path,monkeypatch):
 harness=load(HARNESS,"oracle_harness_test"); gate=load(MODULE,"oracle_gate_test")
 home=tmp_path/"home"; reports=home/"reports/completion-oracle"; reports.mkdir(parents=True)
 store=harness.CompletionStore(home/"completion")
 prompt=store.archive_prompt("Do everything",source="discord",session_id="s",profile="operator")
 mission=store.create_mission("MISS-ORACLE",[prompt]); req=store.add_requirement(prompt,"Do everything",mission_id=mission)
 store.add_evidence(mission,req,"pytest","PASS","pytest-report"); store.set_requirement_status(req,"VERIFIED")
 ledger=store.ledger_digest(mission); store.close()
 report=reports/"MISS-ORACLE.json"; report.write_text(json.dumps({"mission_id":mission,"classification":"COMPLETE","requirements_verified":True,"gauntlet":"PASS","ledger_sha256":ledger})); report.chmod(0o600)
 monkeypatch.setattr(gate.os,"geteuid",lambda:0); monkeypatch.setattr(gate,"HARNESS_PATH",HARNESS); monkeypatch.setattr(gate,"ORACLE_ROOT",tmp_path/"oracle"); monkeypatch.setitem(gate.PROFILES,"operator",("operator",str(home)))
 monkeypatch.setattr(gate,"_expected_owner",lambda _user: (gate.os.getuid(),gate.os.getgid()))
 monkeypatch.setattr(harness.os,"geteuid",lambda:0); monkeypatch.setattr(harness,"_authority_uid",lambda:harness.os.getuid()); monkeypatch.setattr(gate,"_harness",lambda:harness)
 result=gate.apply_oracle_pass("operator",mission,report,"completion-oracle")
 assert result["mission_id"]==mission
 check=harness.CompletionStore(home/"completion",oracle_root=tmp_path/"oracle",profile="operator").completion_gate(mission)
 assert check["completion_oracle_passed"] is True
 assert check["permit_done"] is True


def test_oracle_gate_rejects_noncomplete_verdict(tmp_path,monkeypatch):
 gate=load(MODULE,"oracle_gate_reject_test"); report=tmp_path/"report.json"; report.write_text('{"mission_id":"M","classification":"PARTIAL","requirements_verified":true,"gauntlet":"PASS"}')
 monkeypatch.setattr(gate.os,"geteuid",lambda:0); monkeypatch.setitem(gate.PROFILES,"operator",("operator",str(tmp_path)))
 monkeypatch.setattr(gate,"_expected_owner",lambda _user: (gate.os.getuid(),gate.os.getgid()))
 try: gate.apply_oracle_pass("operator","M",report,"oracle")
 except ValueError as error: assert "complete" in str(error).lower()
 else: raise AssertionError("PARTIAL oracle verdict must be rejected")
