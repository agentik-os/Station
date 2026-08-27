#!/usr/bin/env bash
set -euo pipefail

install_root=${AGK_TERMINAL_ROOT:-/usr/local/lib/agk-terminal}
hermes_home=${HERMES_HOME:-${HOME:?}/.hermes}
case "$hermes_home" in
  ""|/) echo "refusing unsafe HERMES_HOME: ${hermes_home:-<empty>}" >&2; exit 2 ;;
esac
agent_source_root=$install_root/agents
if [ ! -d "$agent_source_root" ]; then
  agent_source_root=$install_root/hermes/agents
fi
resolve_executable() {
  local path=$1 target
  while [ -L "$path" ]; do
    target=$(readlink "$path")
    case "$target" in
      /*) path=$target ;;
      *) path=$(dirname "$path")/$target ;;
    esac
  done
  printf '%s/%s\n' "$(cd "$(dirname "$path")" && pwd -P)" "$(basename "$path")"
}

hermes_bin=$(resolve_executable "$(command -v hermes)")

mkdir -p "$hermes_home/plugins" "$hermes_home/agents" \
  "$hermes_home/dashboard-themes"
mkdir -p "$HOME/.local/bin"
ln -sfn "$hermes_bin" "$HOME/.local/bin/hermes"
hermes config migrate >/dev/null
# Owner-trusted AGK deployment: never pause interactive, Discord, cron, or
# one-shot sessions for approval dialogs. Hermes' non-bypassable hardline
# catastrophic-command floor remains active, and secret redaction stays on.
hermes config set approvals.mode off >/dev/null
hermes config set approvals.cron_mode approve >/dev/null
hermes config set approvals.single_query_mode approve >/dev/null
hermes config set approvals.mcp_reload_confirm false >/dev/null
hermes config set approvals.destructive_slash_confirm false >/dev/null
hermes config set security.redact_secrets true >/dev/null
hermes config set model.provider openai-codex >/dev/null
hermes config set model.default gpt-5.6-sol >/dev/null
hermes config set fallback_providers '[]' >/dev/null
hermes config set credential_pool_strategies.openai-codex fill_first >/dev/null
# AGK owns lifecycle health centrally. Routine stop/start chatter is disabled
# on every messaging adapter; the external watchdog emits one Discord #general
# alert only after ten continuous minutes of unavailability.
hermes config set platforms.discord.gateway_restart_notification false >/dev/null
hermes config set platforms.telegram.gateway_restart_notification false >/dev/null
# Keep Discord's stable surface small; evolving actions (including session
# resume) live inside registry-driven Views and therefore need no slash resync.
hermes config set platforms.discord.extra.command_ui_mode ui_only >/dev/null
if [ "$(id -un)" = "operator" ] && [ "$hermes_home" = "/home/operator/.hermes" ]; then
  hermes config set platforms.discord.extra.session_manager_channel_id 1542462952714670190 >/dev/null
  hermes config set platforms.discord.extra.usage_monitor_channel_id 1542505218569150585 >/dev/null
  hermes config set platforms.discord.extra.usage_monitor_openai_channel_id 1542505478679171164 >/dev/null
  hermes config set platforms.discord.extra.usage_monitor_claude_channel_name claudecode-all-accounts >/dev/null
  hermes config set platforms.discord.extra.usage_monitor_interval_seconds 300 >/dev/null
  hermes config set platforms.discord.extra.cleanup_expected_guild_id 1541131439599386644 >/dev/null
  hermes config set platforms.discord.extra.cleanup_channel_ids '["1542308925506850917"]' >/dev/null
fi
# Local, bilingual, high-quality voice processing. The shared Hermes venv owns
# faster-whisper/Piper; profile configuration and downloaded voice/model caches
# remain isolated in each HERMES_HOME.
hermes config set stt.enabled true >/dev/null
hermes config set stt.echo_transcripts true >/dev/null
hermes config set stt.provider local >/dev/null
hermes config set stt.local.model large-v3 >/dev/null
hermes config set stt.language '' >/dev/null
hermes config set stt.local.language '' >/dev/null
hermes config set stt.local.device cpu >/dev/null
hermes config set stt.local.compute_type int8 >/dev/null
hermes config set stt.local.vad true >/dev/null
hermes config set stt.prompt 'AGK, Agentik, Hermes, Gareth, Operator, Mission, Private, RMUX, Discord, Codex' >/dev/null
hermes config set tts.provider piper >/dev/null
hermes config set tts.piper.voice fr_FR-siwis-medium >/dev/null
# Cross-session discovery is intentionally stricter than ordinary bot access:
# Hermes requires an explicit slash administrator. Promote only numeric IDs
# already authorized by the profile's own DISCORD_ALLOWED_USERS setting; never
# copy an identity across Linux/profile boundaries and never print it.
discord_admin_json=$(python3 - "$hermes_home/.env" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
value = ""
try:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("DISCORD_ALLOWED_USERS="):
            value = stripped.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            break
except OSError:
    pass
print(json.dumps([item.strip() for item in value.split(",") if item.strip().isdigit()]))
PY
)
if [ "$discord_admin_json" != "[]" ]; then
  hermes config set platforms.discord.extra.allow_admin_from "$discord_admin_json" >/dev/null
  hermes config set platforms.discord.extra.group_allow_admin_from "$discord_admin_json" >/dev/null
fi
for plugin_path in agentik_os agk_discord_ui_policy platforms/discord; do
  plugin_target=$hermes_home/plugins/$plugin_path
  mkdir -p "$(dirname "$plugin_target")"
  rm -rf "$plugin_target.new"
  cp -a "$install_root/hermes/plugins/$plugin_path" "$plugin_target.new"
  rm -rf "$plugin_target"
  mv "$plugin_target.new" "$plugin_target"
done

if [ -d "$install_root/hermes/skills" ]; then
  for skill_source in "$install_root"/hermes/skills/*; do
    [ -d "$skill_source" ] || continue
    skill_target=$hermes_home/skills/$(basename "$skill_source")
    rm -rf "$skill_target.new"
    cp -a "$skill_source" "$skill_target.new"
    rm -rf "$skill_target"
    mv "$skill_target.new" "$skill_target"
  done
fi

if [ -d "$agent_source_root" ]; then
  for agent_source in "$agent_source_root"/*; do
    [ -d "$agent_source" ] || continue
    agent_target=$hermes_home/agents/$(basename "$agent_source")
    rm -rf "$agent_target.new"
    cp -a "$agent_source" "$agent_target.new"
    rm -rf "$agent_target"
    mv "$agent_target.new" "$agent_target"
  done
fi

install -m 0644 \
  "$install_root/hermes/dashboard-themes/agentik-shadcn.yaml" \
  "$hermes_home/dashboard-themes/agentik-shadcn.yaml"
install -m 0644 \
  "$install_root/hermes/dashboard-themes/agentik-shadcn-light.yaml" \
  "$hermes_home/dashboard-themes/agentik-shadcn-light.yaml"

for plugin_path in agentik_os agk_discord_ui_policy platforms/discord; do
  hermes plugins doctor --ci "$hermes_home/plugins/$plugin_path" >/dev/null
done
hermes plugins enable --no-allow-tool-override agentik-os >/dev/null
hermes plugins enable --no-allow-tool-override agk-discord-ui-policy >/dev/null
hermes plugins enable --no-allow-tool-override platforms/discord >/dev/null
"$install_root/venv/bin/python" "$install_root/scripts/sync-power-stack.py" \
  --manifest "$install_root/config/power-stack.yaml" \
  --builder "$install_root/scripts/build-agency-skill.py"
if command -v caveman >/dev/null 2>&1; then
  # Signed companion binaries are profile-local and installed atomically after
  # checksum/signature verification by the pinned Caveman CLI.
  caveman setup --install >/dev/null
  caveman setup --json >/dev/null
  caveman tools mcp install hermes >/dev/null
fi
hermes skills list --source builtin >/dev/null
rules_python=python3
if ! python3 -c 'import yaml' >/dev/null 2>&1; then
  rules_python=$install_root/venv/bin/python
fi
"$rules_python" "$install_root/scripts/sync-rules.py" >/dev/null
echo "Hermes extensions synchronized in $hermes_home"
