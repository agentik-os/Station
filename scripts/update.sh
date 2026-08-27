#!/usr/bin/env bash
set -euo pipefail
ref=main
while [ "$#" -gt 0 ]; do case "$1" in --ref) ref=${2:?missing ref}; shift 2;; -h|--help) echo 'usage: sudo station update [--ref REF]'; exit 0;; *) echo "unknown option: $1" >&2; exit 2;; esac; done
case "$ref" in *[!A-Za-z0-9._/-]*|"") echo 'invalid ref' >&2; exit 2;; esac
export STATION_REF=$ref
exec bash /opt/station/current/install --ref "$ref"
