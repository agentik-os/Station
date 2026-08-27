import importlib.util
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_station_manifest_pins_upstreams_and_product_boundary():
    manifest = yaml.safe_load((ROOT / "station.yaml").read_text())
    assert manifest["product"]["id"] == "station"
    assert manifest["product"]["version"] == "0.3.0"
    for component in ("agk_tui", "hermes"):
        commit = manifest["components"][component]["commit"]
        assert re.fullmatch(r"[0-9a-f]{40}", commit)
    boundaries = " ".join(manifest["boundaries"])
    assert "AGK-TUI owns RMUX mapping" in boundaries
    assert "Station owns the complete portable product" in boundaries


def test_online_installer_and_bootstrap_are_safe_and_complete():
    online = (ROOT / "install").read_text()
    bootstrap = (ROOT / "bootstrap-vps.sh").read_text()
    assert "codeload.github.com/$repository/tar.gz/$ref" in online
    assert "--proto '=https'" in online and "--tlsv1.2" in online
    assert "unsafe archive" in online
    assert "overlay" in bootstrap
    assert "bootstrap-vps.sh" in bootstrap
    assert "install-hermes-fleet-dashboard.sh" in bootstrap
    assert "completion_harness.py" in (ROOT / "overlay" / "install.sh").read_text()
    assert "agk-recovery-auditor.timer" in (ROOT / "overlay" / "install.sh").read_text()
    assert "agk_discord_ui_policy" in (ROOT / "overlay" / "install.sh").read_text()
    assert "station doctor" in bootstrap
    assert "DISCORD_BOT_TOKEN" not in bootstrap
    for path in (ROOT / "install", ROOT / "bootstrap-vps.sh"):
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_station_cli_exposes_lifecycle_and_secure_discord_rotation():
    source = (ROOT / "bin" / "station").read_text()
    for command in ("doctor", "status", "tui", "portal", "discord", "backup", "update", "rollback"):
        assert re.search(rf"\b{command}(?:\||\))", source)
    assert "rotate_discord_token.py" in source
    assert "sudo" in source


def test_discord_rotation_registry_covers_station_bots():
    path = ROOT / "overlay" / "scripts" / "rotate_discord_token.py"
    spec = importlib.util.spec_from_file_location("station_token_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert set(module.TARGETS) == {"operator", "agentik", "mission", "private", "collective", "nutrition-os"}
