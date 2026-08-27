#!/usr/bin/env python3
"""Create a local mode-0600 Station recovery archive without printing secrets."""
from __future__ import annotations
import argparse, json, os, subprocess, tarfile, tempfile, time
from pathlib import Path, PurePosixPath

BASE_PATHS = [
    "/etc/agk-terminal", "/etc/agentik", "/var/lib/agk-terminal",
    "/home/operator/.config/systemd/user", "/home/agentik/.config/systemd/user",
    "/home/mission/.config/systemd/user", "/home/private/.config/systemd/user",
]
PROFILE_FILES = (".hermes/config.yaml", ".hermes/.env", ".hermes/auth.json", ".agentik/os-assignments.yaml")

def main() -> int:
    parser=argparse.ArgumentParser(); parser.add_argument("--output-dir", default="/var/backups/station"); parser.add_argument("--full-state", action="store_true"); args=parser.parse_args()
    if os.geteuid()!=0: raise SystemExit("Station backup requires root")
    output=Path(args.output_dir); output.mkdir(parents=True, exist_ok=True); output.chmod(0o700)
    stamp=time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()); target=output/f"station-{stamp}.tar.gz"
    paths=[Path(p) for p in BASE_PATHS]
    for user in ("operator","agentik","mission","private"):
        paths.extend(Path(f"/home/{user}")/item for item in PROFILE_FILES)
        if args.full_state: paths.extend([Path(f"/home/{user}/.hermes/state.db"),Path(f"/home/{user}/.hermes/kanban")])
    existing=[p for p in paths if p.exists() and not p.is_symlink()]
    with tempfile.NamedTemporaryFile("w",encoding="utf-8",delete=False) as handle:
        manifest=Path(handle.name); json.dump({"schema":"station.backup.v1","created_at":stamp,"full_state":args.full_state,"paths":[str(p) for p in existing]},handle); handle.write("\n")
    try:
        command=["tar","-czf",str(target),"--absolute-names","--transform=s,^/,rootfs/,"]+[str(p) for p in existing]+[str(manifest)]
        subprocess.run(command,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        target.chmod(0o600)
        with tarfile.open(target,"r:gz") as archive:
            for member in archive.getmembers():
                path=PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts: raise RuntimeError("unsafe backup member")
        print(f"Station backup created: {target} ({target.stat().st_size} bytes)")
    finally: manifest.unlink(missing_ok=True)
    return 0
if __name__=="__main__": raise SystemExit(main())
