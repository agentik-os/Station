from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "overlay/hermes/plugins/agk_discord_ui_policy/__init__.py"


def _policy_prompt() -> str:
    spec = spec_from_file_location("agk_discord_ui_policy", POLICY)
    assert spec is not None
    module = module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.policy_prompt()


def test_owner_policy_forbids_decorative_full_message_quote_rails():
    prompt = _policy_prompt()

    assert "Do not wrap ordinary replies in full-message Discord blockquotes (`>>>`)" in prompt
    assert "Do not use colored accent rails as decoration" in prompt


def test_owner_questions_are_single_and_self_contained():
    prompt = _policy_prompt().lower()

    assert "sole visible question" in prompt
    assert "context" in prompt
    assert "decision" in prompt
    assert "exact target" in prompt
    assert "consequences" in prompt
    assert "do not preface" in prompt


def test_sync_persists_quiet_discord_action_message_contract():
    sync = (ROOT / "overlay/scripts/sync-hermes.sh").read_text()
    required = {
        "display.platforms.discord.tool_progress off",
        "display.platforms.discord.tool_progress_grouping accumulate",
        "display.platforms.discord.interim_assistant_messages false",
        "display.platforms.discord.long_running_notifications false",
        "display.platforms.discord.busy_ack_detail false",
        "display.platforms.discord.busy_steer_ack_enabled false",
        "display.platforms.discord.streaming false",
        "display.platforms.discord.action_messages true",
    }
    for setting in required:
        assert f"hermes config set {setting}" in sync
    assert 'hermes_home=${HERMES_HOME:-${HOME:?}/.hermes}' in sync
    assert 'for plugin_path in agentik_os agk_discord_ui_policy platforms/discord' in sync
    assert 'mkdir -p "$(dirname "$discord_target")"' in sync
    assert 'cp -a "$discord_source" "$discord_target.new"' in sync
    assert 'cp -a "$discord_target"/. "$discord_target.new"/' in sync
    assert "adapter.py command_center.py interaction_surfaces.py" in sync
    assert 'install -m 0644 "$discord_source/$common_file"' in sync
    assert 'rm -rf "$discord_target"' in sync
    assert 'mv "$discord_target.new" "$discord_target"' in sync
    assert 'for agent_source in "$agent_source_root"/*' in sync
    assert 'agent_target=$hermes_home/agents/$(basename "$agent_source")' in sync


def test_sync_persists_quiet_telegram_action_message_contract():
    sync = (ROOT / "overlay/scripts/sync-hermes.sh").read_text()
    required = {
        "display.platforms.telegram.tool_progress off",
        "display.platforms.telegram.tool_progress_grouping accumulate",
        "display.platforms.telegram.interim_assistant_messages false",
        "display.platforms.telegram.long_running_notifications false",
        "display.platforms.telegram.busy_ack_detail false",
        "display.platforms.telegram.busy_steer_ack_enabled false",
        "display.platforms.telegram.streaming false",
        "display.platforms.telegram.action_messages true",
        "display.platforms.telegram.notifications important",
    }
    for setting in required:
        assert f"hermes config set {setting}" in sync


def test_gateway_projects_station_action_messages_on_discord_and_telegram():
    gateway = (ROOT / "overlay/hermes-core/gateway/run.py").read_text()
    assert "source.platform in {Platform.DISCORD, Platform.TELEGRAM}" in gateway


def test_owner_policy_requires_full_live_canonical_plan_checklist():
    standard = (ROOT / "overlay/hermes/plugins/agk_discord_ui_policy/STANDARD.md").read_text()
    assert "Show every canonical plan action in the live Action Message" in standard
    assert "completed, in progress, pending, or cancelled" in standard
    assert "plan progress must edit the same message" in standard.lower()
    normalized = standard.lower()
    assert "before operational execution begins" in normalized
    assert "every currently known action" in normalized


def test_owner_policy_applies_same_plan_message_without_telegram_notification_storms():
    standard = (ROOT / "overlay/hermes/plugins/agk_discord_ui_policy/STANDARD.md").read_text()
    normalized = standard.lower()
    assert "discord and telegram" in normalized
    assert "same message" in normalized
    assert "notification storm" in normalized
    assert "before operational execution begins" in normalized


def test_shared_hermes_refresh_syncs_every_named_profile_not_only_configured_profiles():
    installer = (ROOT / "overlay/scripts/install-shared-hermes.sh").read_text()
    assert 'for user_name in "${users[@]}"' in installer
    assert 'profiles_root="$home_dir/.hermes/profiles"' in installer
    assert "find \"$profiles_root\" -mindepth 1 -maxdepth 1 -type d -print0" in installer
    assert 'HERMES_HOME="$profile_home"' in installer
    assert '"$install_root/scripts/sync-hermes.sh"' in installer


def test_owner_policy_pins_editorial_minimal_action_message_marks():
    standard = (ROOT / "overlay/hermes/plugins/agk_discord_ui_policy/STANDARD.md").read_text()
    assert "Editorial minimal" in standard
    assert "`✓` completed" in standard
    assert "`→` in progress" in standard
    assert "`·` pending" in standard
    assert "`◇` verifying" in standard
    assert "`‖` waiting or blocked" in standard
    assert "`×` failed" in standard
    assert "`—` cancelled" in standard


def test_owner_policy_uses_one_visible_question_surface():
    prompt = _policy_prompt()
    assert "interactive surface as the sole visible question" in prompt
    assert "Do not preface it with a separate assistant message" in prompt
