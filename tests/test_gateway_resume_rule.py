from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_station_rules_require_gateway_reconnect_continuation():
    document = yaml.safe_load((ROOT / "overlay/config/rules.yaml").read_text(encoding="utf-8"))
    rules = {rule["id"]: rule for rule in document["rules"]}
    rule = rules["resume-interrupted-work-after-gateway-reconnect"]
    content = rule["content"].lower()
    assert rule["enabled"] is True
    assert "automatically resume" in content
    assert "durable" in content
    assert "restart/shutdown" in content
    assert "explicitly stops" in content
    assert "first unfinished step" in content
