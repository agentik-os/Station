import importlib.util
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
RULES_MODULE = ROOT / "overlay/hermes/plugins/agentik_os/rules.py"


def _rules(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))["rules"]


def test_station_rule_defines_three_peer_directors_without_operator_bottleneck():
    by_id = {rule["id"]: rule for rule in _rules(ROOT / "overlay/config/rules.yaml")}
    rule = by_id["station-three-directors-authority"]
    content = rule["content"]
    assert rule["enabled"] is True
    assert all(name in content for name in ("Operator", "Agentik", "Private"))
    assert "without asking Operator" in content
    assert "Operator performs the bounded mechanics" in content
    assert "Escalate directly to Gareth" in content
    assert "never crosses client boundaries" in content


def test_team_rule_no_longer_calls_operator_global_admin():
    by_id = {rule["id"]: rule for rule in _rules(ROOT / "overlay/config/rules.yaml")}
    content = by_id["station-capabilities-and-team-communication"]["content"]
    assert "Operator is global admin" not in content
    assert "peer Station Directors" in content


def test_hermes_rule_section_is_bounded_and_keeps_director_authority(monkeypatch):
    spec = importlib.util.spec_from_file_location("director_rules", RULES_MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("AGK_RULES_CONFIG", str(ROOT / "overlay/config/rules.yaml"))
    prompt = module.rules_prompt()
    assert len(prompt) <= 900
    assert "peer Station Directors" in prompt
    assert "A0-A3" in prompt
    assert "complete-request-ledger" not in prompt
