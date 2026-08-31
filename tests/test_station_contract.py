import importlib.util
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_station_manifest_pins_upstreams_and_product_boundary():
    manifest = yaml.safe_load((ROOT / "station.yaml").read_text())
    assert manifest["product"]["id"] == "station"
    assert manifest["product"]["version"] == "0.4.3"
    assert manifest["components"]["hermes"]["commit"] == "f896b386d06a11c47784a5a5676c1be31945048e"
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
    overlay_install = (ROOT / "overlay" / "install.sh").read_text()
    assert "agk_discord_ui_policy" in overlay_install
    assert "station_loopback_host_proxy.py" in overlay_install
    assert "agk-private-dashboard-proxy.service" in overlay_install
    shared=(ROOT / "overlay" / "scripts" / "install-shared-hermes.sh").read_text()
    assert "DISCORD_ALLOW_BOTS=mentions" in shared
    assert "DISCORD_BOTS_REQUIRE_INLINE_MENTION=true" in shared
    assert "agent.restart_drain_timeout 1800" in shared
    assert "agent.restart_after_turn_timeout 1800" in shared
    assert "TimeoutStopSec=1860" in shared
    safe_reload=(ROOT/"overlay/scripts/station_safe_gateway_reload.py").read_text()
    assert "write_drain_request" in safe_reload and "clear_drain_request" in safe_reload
    assert "active work did not drain; reload cancelled without interrupting it" in safe_reload
    assert "'status':'not-running'" in safe_reload
    assert "RestartUnit" not in safe_reload and " restart " not in safe_reload
    watchdog=(ROOT/"overlay/scripts/gateway_watchdog.py").read_text()
    assert "attempt_recovery" in watchdog and "is-enabled" in watchdog
    assert ".drain_request.json" in watchdog and '"start", unit' in watchdog
    assert "1541816910587625492,1541817649661747351,1541817976586637382,1541817162241540126,1541131574509314209" in shared
    broker=(ROOT/"overlay/scripts/station_interagent_broker.py").read_text()
    dispatch=(ROOT/"overlay/scripts/station_interagent_work_dispatch.py").read_text()
    assert "ThreadPoolExecutor(max_workers=3" in broker and "queue_interagent_work(record)" in broker
    assert "post_handoff" in dispatch and "wait_for_bot_reply" in dispatch
    assert "RuntimeLauncher()" not in dispatch
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


def test_nutrition_safe_reload_targets_canonical_private_runtime():
    station = (ROOT / "bin/station").read_text()
    assert "nutrition-os) user=private; unit=hermes-gateway-nutrition-os.service; home=/home/private/.hermes/profiles/nutrition-os" in station
    assert "nutrition-os) user=operator; unit=hermes-gateway-nutrition-os.service; home=/home/operator/.hermes/profiles/nutrition-os" not in station


def test_every_station_registry_owns_nutrition_under_private():
    paths = [
        ROOT / "overlay/scripts/rotate_discord_token.py",
        ROOT / "overlay/scripts/completion_oracle_gate.py",
        ROOT / "overlay/scripts/recovery_router.py",
        ROOT / "overlay/scripts/completion_harness.py",
        ROOT / "overlay/scripts/approval_gate.py",
        ROOT / "overlay/scripts/fleet_recovery_auditor.py",
        ROOT / "overlay/hermes/plugins/agentik_os/nutrition_command.py",
    ]
    for path in paths:
        source = path.read_text()
        assert "/home/private/.hermes/profiles/nutrition-os" in source, path
        assert "/home/operator/.hermes/profiles/nutrition-os" not in source, path


def test_nutrition_specialist_is_private_owned_without_legacy_launcher():
    manifest = (ROOT / "overlay/hermes/agents/nutrition-specialist/agent.yaml").read_text()
    prompt = (ROOT / "overlay/hermes/agents/nutrition-specialist/prompt.md").read_text()
    assert "owner_environment: private" in manifest
    assert "scope: [private]" in manifest
    assert "profile: nutrition-os" in manifest
    assert "launcher:" not in manifest
    assert "`nutrition-os` Hermes profile under the Private Linux boundary" in prompt
    assert "dedicated `nutrition` Hermes profile" not in prompt


def test_specialized_agents_have_one_canonical_owner_and_valid_profile_binding():
    agents = ROOT / "overlay/hermes/agents"
    expected = {
        "completion-oracle": ("operator", None),
        "recovery-auditor": ("operator", None),
        "master-os-builder": ("operator", "builder-os"),
        "nutrition-specialist": ("private", "nutrition-os"),
    }
    for agent_id, (owner, profile) in expected.items():
        manifest = yaml.safe_load((agents / agent_id / "agent.yaml").read_text())
        assert manifest["owner_environment"] == owner
        assert manifest["scope"] == [owner]
        assert manifest.get("profile") == profile
        assert not manifest.get("launcher")
