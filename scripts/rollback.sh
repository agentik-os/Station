#!/usr/bin/env bash
set -euo pipefail
station_root=/opt/station
requested=${1:-}
if [ -z "$requested" ]; then
  [ -L "$station_root/previous" ] || { echo 'No previous Station release is recorded' >&2; exit 1; }
  target=$(readlink -f "$station_root/previous")
else
  case "$requested" in *[!A-Za-z0-9._-]*|"") echo 'invalid release' >&2; exit 2;; esac
  target=$station_root/releases/$requested
fi
[ -d "$target" ] && [ -f "$target/station.yaml" ] || { echo "Station release not found: $target" >&2; exit 1; }
current=$(readlink -f "$station_root/current" 2>/dev/null || true)
[ -z "$current" ] || ln -sfn "releases/$(basename "$current")" "$station_root/previous"
ln -sfn "releases/$(basename "$target")" "$station_root/current"
ln -sfn /opt/station/current /usr/local/lib/station
ln -sfn /opt/station/current/bin/station /usr/local/bin/station
exec bash "$target/bootstrap-vps.sh" --core-only
