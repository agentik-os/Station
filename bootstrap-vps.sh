#!/usr/bin/env bash
set -euo pipefail

source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
dry_run=false
core_only=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) dry_run=true; shift ;;
    --core-only) core_only=true; shift ;;
    -h|--help) echo "usage: sudo ./bootstrap-vps.sh [--dry-run] [--core-only]"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(uname -s)" = Linux ] || { echo "Station requires Linux" >&2; exit 1; }
if [ "$dry_run" = false ] && [ "$(id -u)" -ne 0 ]; then
  echo "Station bootstrap requires root" >&2
  exit 1
fi

version_file=$source_root/config/versions.lock
[ -f "$version_file" ] || { echo "versions.lock is missing" >&2; exit 1; }
while IFS='=' read -r key value; do
  case "$key" in
    AGK_TUI_REPOSITORY) agk_repository=$value ;;
    AGK_TUI_COMMIT) agk_commit=$value ;;
    HERMES_COMMIT) hermes_commit=$value ;;
    RMUX_VERSION) rmux_version=$value ;;
    STATION_VERSION) station_version=$value ;;
  esac
done < "$version_file"
for value in "$agk_commit" "$hermes_commit"; do
  [[ "$value" =~ ^[0-9a-f]{40}$ ]] || { echo "invalid immutable commit pin" >&2; exit 2; }
done
[[ "$agk_repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "invalid AGK-TUI repository" >&2; exit 2; }
[[ "$station_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "invalid Station version" >&2; exit 2; }

station_root=/opt/station
release_id="v${station_version}-$(sha256sum "$source_root/station.yaml" | cut -c1-12)"
release_dir=$station_root/releases/$release_id
agk_source=$station_root/sources/agk-tui-$agk_commit

phase() { printf '\n==> %s\n' "$1"; }
if [ "$dry_run" = true ]; then
  cat <<EOF
Station $station_version dry run
  AGK-TUI:  $agk_repository@$agk_commit
  Hermes:   NousResearch/hermes-agent@$hermes_commit
  RMUX:     $rmux_version
  Release:  $release_dir
  Profiles: operator, agentik, mission, private
  Portal:   loopback + Tailscale Serve
  Discord:  owner-controlled token setup after install
EOF
  echo "  + download immutable AGK-TUI archive"
  echo "  + apply Station overlay"
  echo "  + run AGK-TUI VPS bootstrap"
  echo "  + install Fleet portal, Kanban, OS packages and lifecycle CLI"
  exit 0
fi

phase "Stage immutable AGK-TUI core"
install -d -m 0755 "$station_root/sources" "$station_root/releases"
if [ ! -d "$agk_source" ]; then
  temporary=$(mktemp -d -t station-agk.XXXXXX)
  trap 'rm -rf -- "$temporary"' EXIT
  archive=$temporary/agk-tui.tar.gz
  curl --proto '=https' --tlsv1.2 --retry 4 --retry-all-errors -fsSL \
    "https://codeload.github.com/$agk_repository/tar.gz/$agk_commit" -o "$archive"
  python3 - "$archive" <<'PY'
import sys, tarfile
from pathlib import PurePosixPath
with tarfile.open(sys.argv[1], 'r:gz') as archive:
    members=archive.getmembers()
    if not members or len(members)>30000: raise SystemExit('unsafe AGK-TUI archive')
    for member in members:
        path=PurePosixPath(member.name)
        if path.is_absolute() or '..' in path.parts or member.issym() or member.islnk():
            raise SystemExit('unsafe AGK-TUI archive path')
PY
  tar -xzf "$archive" -C "$temporary"
  extracted=$(python3 - "$temporary" <<'PY'
import pathlib,sys
root=pathlib.Path(sys.argv[1]); rows=[p for p in root.iterdir() if p.is_dir() and (p/'bootstrap-vps.sh').is_file()]
if len(rows)!=1: raise SystemExit('AGK-TUI archive layout is invalid')
print(rows[0])
PY
)
  mv "$extracted" "$agk_source"
  rm -rf "$temporary"
  trap - EXIT
fi

phase "Apply the Station product overlay"
cp -a "$source_root/overlay/." "$agk_source/"

phase "Install AGK-TUI RMUX mapping and shared Hermes runtime"
agk_args=()
[ "$core_only" = true ] && agk_args+=(--core-only)
env HERMES_OFFICIAL_COMMIT="$hermes_commit" RMUX_VERSION="$rmux_version" \
  bash "$agk_source/bootstrap-vps.sh" "${agk_args[@]}"

phase "Install Station Portal, Kanban, Operative Systems and Discord lifecycle"
bash "$agk_source/scripts/install-hermes-fleet-dashboard.sh" --source-root "$agk_source"

phase "Publish the local Station release"
if [ ! -d "$release_dir" ]; then
  staging=$(mktemp -d "$station_root/releases/.station.XXXXXX")
  cp -a "$source_root/." "$staging/"
  rm -rf "$staging/.git"
  chown -R root:root "$staging"
  find "$staging" -type d -exec chmod 0755 {} +
  find "$staging" -type f -exec chmod 0644 {} +
  chmod 0755 "$staging/install" "$staging/bootstrap-vps.sh" "$staging/bin/station" "$staging/scripts/"*.sh "$staging/scripts/"*.py
  mv "$staging" "$release_dir"
fi
if [ -L "$station_root/current" ]; then
  prior=$(readlink -f "$station_root/current" || true)
  if [ -n "$prior" ] && [ "$prior" != "$release_dir" ]; then
    ln -sfn "releases/$(basename "$prior")" "$station_root/previous"
  fi
fi
ln -sfn "releases/$release_id" "$station_root/current"
ln -sfn /opt/station/current /usr/local/lib/station
ln -sfn /opt/station/current/bin/station /usr/local/bin/station
install -d -m 0750 -o root -g operator /var/lib/station /var/backups/station
printf '{"version":"%s","release":"%s","agk_tui_commit":"%s","hermes_commit":"%s"}\n' \
  "$station_version" "$release_id" "$agk_commit" "$hermes_commit" > /var/lib/station/state.json
chmod 0640 /var/lib/station/state.json
chown root:operator /var/lib/station/state.json

phase "Verify Station"
/usr/local/bin/station doctor
cat <<'EOF'

Station is installed.
  station status
  station tui
  station portal
  sudo station discord token list
  sudo station discord token rotate <target>
EOF
