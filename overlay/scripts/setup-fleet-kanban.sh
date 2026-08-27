#!/usr/bin/env bash
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "setup-fleet-kanban.sh must run as root" >&2; exit 1; }
hermes_bin=${HERMES_BIN:-/opt/agk-terminal/hermes-agent/venv/bin/hermes}
[ -x "$hermes_bin" ] || { echo "Hermes is unavailable: $hermes_bin" >&2; exit 1; }

for profile in operator agentik mission private; do
  home_dir=$(getent passwd "$profile" | cut -d: -f6)
  [ "$home_dir" = "/home/$profile" ] || { echo "Unsafe home for $profile" >&2; exit 1; }
  slug="$profile-station"
  name="${profile^} Station"
  description="AGK station board for $profile workflows, sessions, agents and Operative Systems"
  current=$(sudo -u "$profile" env HOME="$home_dir" HERMES_HOME="$home_dir/.hermes" \
    "$hermes_bin" kanban boards show 2>/dev/null || true)
  if ! sudo -u "$profile" env HOME="$home_dir" HERMES_HOME="$home_dir/.hermes" \
    "$hermes_bin" kanban boards list 2>/dev/null | grep -Eq "(^|[[:space:]])$slug([[:space:]]|$)"
  then
    sudo -u "$profile" env HOME="$home_dir" HERMES_HOME="$home_dir/.hermes" \
      "$hermes_bin" kanban boards create "$slug" \
      --name "$name" --description "$description" --icon '◆' \
      --color '#7170ff' --default-workdir "$home_dir/workspace"
  fi
  sudo -u "$profile" env HOME="$home_dir" HERMES_HOME="$home_dir/.hermes" \
    "$hermes_bin" kanban boards switch "$slug" >/dev/null
  sudo -u "$profile" env HOME="$home_dir" HERMES_HOME="$home_dir/.hermes" \
    "$hermes_bin" config set kanban.dispatch_in_gateway true >/dev/null
  sudo -u "$profile" env HOME="$home_dir" HERMES_HOME="$home_dir/.hermes" \
    "$hermes_bin" config set kanban.review_dispatch true >/dev/null
  sudo -u "$profile" env HOME="$home_dir" HERMES_HOME="$home_dir/.hermes" \
    "$hermes_bin" config set kanban.auto_decompose true >/dev/null
  echo "Kanban ready: $profile/$slug${current:+ (previous: $current)}"
done
