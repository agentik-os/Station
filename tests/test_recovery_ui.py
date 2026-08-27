import importlib.util
import sqlite3
from pathlib import Path

MODULE = Path(__file__).resolve().parents[1] / "overlay/hermes/plugins/platforms/discord/agk_recovery_ui.py"


def load():
    spec=importlib.util.spec_from_file_location("agk_recovery_ui_test",MODULE); assert spec and spec.loader
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def test_recovery_custom_ids_are_typed_and_bounded():
    module=load()
    assert module.parse_recovery_custom_id("agk_recovery:RELAUNCH:FIND-1234567890abcdef") == ("RELAUNCH","FIND-1234567890abcdef")
    for bad in ("agk_recovery:RUN:../../etc", "agk_recovery:RELAUNCH:x", "other:RELAUNCH:FIND-1234567890abcdef"):
        try: module.parse_recovery_custom_id(bad)
        except ValueError: pass
        else: raise AssertionError(bad)


def test_controller_reads_operator_findings_without_prompt_content(tmp_path):
    module=load(); root=tmp_path/"completion"; root.mkdir(); db=sqlite3.connect(root/"completion.db")
    db.executescript("CREATE TABLE missions(id TEXT PRIMARY KEY,project TEXT,state TEXT); CREATE TABLE findings(id TEXT PRIMARY KEY,mission_id TEXT,classification TEXT,severity TEXT,requirement_ids_json TEXT,human_decision TEXT,created_at TEXT,updated_at TEXT); INSERT INTO missions VALUES('MISS-1','station','ACTIVE'); INSERT INTO findings VALUES('FIND-1234567890abcdef','MISS-1','INCOMPLETE','P1','[]',NULL,'now','now');")
    db.commit(); db.close()
    controller=module.RecoveryController(completion_root=root, fleet_index=tmp_path/"missing.json", runner=lambda *a:None)
    rows=controller.list_findings()
    assert rows[0]["finding_id"]=="FIND-1234567890abcdef"
    assert "prompt" not in rows[0]


def test_mission_context_redacts_secret_like_prompt(tmp_path):
    module=load(); root=tmp_path/"completion"; (root/"relaunch").mkdir(parents=True)
    (root/"relaunch"/"FIND-1234567890abcdef.json").write_text('{"mission_id":"MISS-1","original_prompts":[{"content":"API_KEY=super-secret-value"}],"requirements":[{"id":"REQ-1","status":"PENDING","text":"password=hidden-value"}]}')
    controller=module.RecoveryController(completion_root=root,fleet_index=tmp_path/"none",runner=lambda *a:None)
    context=controller.safe_mission_context("FIND-1234567890abcdef")
    assert "super-secret-value" not in context
    assert "hidden-value" not in context
    assert module._safe_discord_text("postgres://admin:privatepass@db.internal/app", "WITHHELD") == "WITHHELD"
    assert "withheld from Discord" in context
