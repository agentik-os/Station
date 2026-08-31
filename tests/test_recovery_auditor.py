import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "overlay/scripts/recovery_auditor.py"


def load():
    spec = importlib.util.spec_from_file_location("recovery_auditor_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_terminal_finding_decision_prevents_duplicate_on_next_audit(tmp_path):
    module = load()
    store = module.CompletionStore(tmp_path / "completion")
    prompt = store.archive_prompt(
        "Implement the bounded item.",
        source="test",
        session_id="session-1",
        profile="operator",
    )
    mission = store.create_mission("MISS-terminal-decision", [prompt])
    store.add_requirement(prompt, "Implement the bounded item.", mission_id=mission)

    module._write_reports(store, "operator", tmp_path / "reports", False, 1)
    finding = store.db.execute(
        "SELECT id FROM findings WHERE mission_id=?", (mission,)
    ).fetchone()
    assert finding is not None
    store.db.execute(
        "UPDATE findings SET human_decision='IGNORE' WHERE id=?", (finding["id"],)
    )
    store.db.commit()

    module._write_reports(store, "operator", tmp_path / "reports", False, 1)
    rows = store.db.execute(
        "SELECT id,human_decision FROM findings WHERE mission_id=?", (mission,)
    ).fetchall()
    assert [(row["id"], row["human_decision"]) for row in rows] == [
        (finding["id"], "IGNORE")
    ]
    store.close()


def test_malformed_historical_identity_is_reported_without_aborting_audit(tmp_path):
    module = load()
    store = module.CompletionStore(tmp_path / "completion")
    prompt = store.archive_prompt(
        "Review the malformed historical requirement.",
        source="test",
        session_id="session-malformed",
        profile="operator",
    )
    mission = store.create_mission("MISS-malformed-history", [prompt])
    requirement = store.add_requirement(
        prompt,
        "Review the malformed historical requirement.",
        mission_id=mission,
        human_gate=True,
    )
    store.db.execute(
        "UPDATE requirements SET id=? WHERE id=?",
        ("malformed/requirement", requirement),
    )
    store.db.commit()

    findings = module._classify(store)

    assert len(findings) == 1
    assert findings[0]["mission_id"] == mission
    assert findings[0]["classification"] == "INTEGRITY_ERROR"
    assert findings[0]["permit_done"] is False
    assert findings[0]["integrity_error"] == "invalid_completion_identity"
    store.close()
