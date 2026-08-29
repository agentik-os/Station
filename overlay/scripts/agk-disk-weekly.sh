#!/usr/bin/env bash
set -euo pipefail
exec /home/operator/.hermes/scripts/agk_disk_maintenance.py --mode weekly --max-delete-gb 20
