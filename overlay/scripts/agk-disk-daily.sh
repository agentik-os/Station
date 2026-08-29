#!/usr/bin/env bash
set -euo pipefail
exec /home/operator/.hermes/scripts/agk_disk_maintenance.py --mode daily --max-delete-gb 20
