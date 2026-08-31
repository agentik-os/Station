#!/usr/bin/env python3
"""Run Recovery Auditor inside each Station profile boundary and aggregate metadata."""
from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROFILES = (
    ("operator", "operator", "/home/operator/.hermes"),
    ("agentik", "agentik", "/home/agentik/.hermes"),
    ("mission", "mission", "/home/mission/.hermes"),
    ("private", "private", "/home/private/.hermes"),
    ("collective", "agentik", "/home/agentik/.hermes/profiles/collective"),
    ("nutrition-os", "private", "/home/private/.hermes/profiles/nutrition-os"),
)


def _run_profile(name: str, user: str, home: str, baseline: bool) -> dict:
    account = pwd.getpwnam(user)
    command = [
        "sudo", "-u", user, "env", f"HOME={account.pw_dir}", f"HERMES_HOME={home}",
        "/usr/local/lib/agk-terminal/venv/bin/python",
        "/usr/local/lib/agk-terminal/scripts/recovery_auditor.py",
        "--profile", name, "--state-db", f"{home}/state.db",
        "--completion-root", f"{home}/completion", "--reports-root", f"{home}/reports", "--json",
    ]
    if baseline:
        command.append("--baseline")
    result = subprocess.run(command, text=True, capture_output=True, timeout=180, check=False)
    if result.returncode != 0:
        return {"profile": name, "status": "ERROR", "error_class": "audit_failed"}
    try:
        row = json.loads(result.stdout)
    except ValueError:
        return {"profile": name, "status": "ERROR", "error_class": "invalid_output"}
    return {
        "profile": name, "status": "OK", "prompts_imported": row.get("prompts_imported", 0),
        "prompts_reviewed": row.get("prompts_reviewed", 0), "missions_reviewed": row.get("missions_reviewed", 0),
        "audit_report": row.get("audit_report", ""), "operator_report": row.get("operator_report", ""),
    }


def audit_fleet(output: Path, baseline: bool = False) -> dict:
    if os.geteuid() != 0:
        raise PermissionError("fleet audit must run as root")
    rows = [_run_profile(*profile, baseline) for profile in PROFILES]
    payload = {
        "schema": "agk.recovery.fleet.v1", "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline": baseline, "profiles": rows,
        "summary": {
            "profiles_ok": sum(row["status"] == "OK" for row in rows),
            "profiles_error": sum(row["status"] != "OK" for row in rows),
            "prompts_reviewed": sum(int(row.get("prompts_reviewed", 0)) for row in rows),
            "missions_reviewed": sum(int(row.get("missions_reviewed", 0)) for row in rows),
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chown(temporary, 0, pwd.getpwnam("operator").pw_gid)
    temporary.chmod(0o640)
    os.replace(temporary, output)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("/var/lib/station/recovery/index.json"))
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = audit_fleet(args.output, args.baseline)
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        summary = payload["summary"]
        print(f"AGK recovery audit: {summary['profiles_ok']}/{len(PROFILES)} profiles, "
              f"{summary['prompts_reviewed']} prompts, {summary['missions_reviewed']} missions")
    return 0 if payload["summary"]["profiles_error"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
