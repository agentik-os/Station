import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "doctor.py"


def load_module():
    spec = importlib.util.spec_from_file_location("station_doctor_test", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_summary_fails_only_required_checks():
    module = load_module()
    checks = [
        module.Check("required-ok", True, True, "ok"),
        module.Check("optional-fail", False, False, "missing"),
    ]
    assert module.exit_code(checks) == 0
    checks.append(module.Check("required-fail", True, False, "broken"))
    assert module.exit_code(checks) == 1


def test_doctor_source_does_not_emit_secret_values():
    source = MODULE.read_text()
    assert "DISCORD_BOT_TOKEN" not in source
    assert "auth.json" not in source
    assert "read_text" not in source
