#!/usr/bin/env python3
"""Root-authoritative bridge from independent Oracle report to mission completion evidence."""
from __future__ import annotations
import argparse, hashlib, importlib.util, json, os, pwd, re
from datetime import datetime, timezone
from pathlib import Path

HARNESS_PATH=Path("/usr/local/lib/agk-terminal/scripts/completion_harness.py")
ORACLE_ROOT=Path("/var/lib/station/recovery/oracle")
PROFILES={
 "operator":("operator","/home/operator/.hermes"),"agentik":("agentik","/home/agentik/.hermes"),
 "mission":("mission","/home/mission/.hermes"),"private":("private","/home/private/.hermes"),
 "collective":("agentik","/home/agentik/.hermes/profiles/collective"),
 "nutrition-os":("private","/home/private/.hermes/profiles/nutrition-os"),
}
_ID=re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

def _expected_owner(user):
 account=pwd.getpwnam(user); return account.pw_uid,account.pw_gid

def _harness():
 spec=importlib.util.spec_from_file_location("trusted_completion_oracle_harness",HARNESS_PATH)
 if spec is None or spec.loader is None: raise RuntimeError("completion harness unavailable")
 module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

def _chown_tree(root,uid,gid):
 for path in Path(root).rglob("*"):
  if path.is_file(): os.chown(path,uid,gid)

def apply_oracle_pass(profile,mission_id,report_path,actor):
 if os.geteuid()!=0: raise PermissionError("Completion Oracle gate requires root")
 if profile not in PROFILES or not _ID.fullmatch(mission_id) or not actor.strip() or len(actor)>100:
  raise ValueError("invalid Oracle gate target")
 report_path=Path(report_path)
 try: payload=json.loads(report_path.read_text(encoding="utf-8"))
 except (OSError,ValueError,TypeError) as exc: raise ValueError("Oracle report is unreadable") from exc
 if (payload.get("mission_id")!=mission_id or payload.get("classification")!="COMPLETE"
     or payload.get("requirements_verified") is not True or payload.get("gauntlet")!="PASS"):
  raise ValueError("Oracle report is not a COMPLETE verified Gauntlet PASS")
 user,home=PROFILES[profile]; home_path=Path(home).resolve(); allowed=(home_path/"reports"/"completion-oracle").resolve()
 resolved=report_path.resolve()
 if allowed not in resolved.parents or report_path.is_symlink() or not report_path.is_file():
  raise ValueError("Oracle report escapes the owning profile report directory")
 uid,gid=_expected_owner(user); st=report_path.stat()
 if st.st_uid!=uid or st.st_mode & 0o022: raise PermissionError("Oracle report owner or permissions are unsafe")
 digest=hashlib.sha256(report_path.read_bytes()).hexdigest(); module=_harness(); store=module.CompletionStore(home_path/"completion",oracle_root=ORACLE_ROOT,profile=profile)
 try:
  exists=store.db.execute("SELECT 1 FROM missions WHERE id=?",(mission_id,)).fetchone()
  if not exists: raise KeyError(mission_id)
  ledger_sha256=store.ledger_digest(mission_id)
  if payload.get("ledger_sha256")!=ledger_sha256: raise ValueError("Oracle report targets a stale or different ledger")
  evidence_id=store.record_oracle_verdict(mission_id,actor=actor,report_sha256=digest,ledger_sha256=ledger_sha256)
  store.event("completeness.passed",mission_id,{"evidence_id":evidence_id,"actor":actor,"profile":profile})
 finally:
  store.close(); _chown_tree(home_path/"completion",uid,gid)
 return {"profile":profile,"mission_id":mission_id,"evidence_id":evidence_id,"sha256":digest,"recorded_at":datetime.now(timezone.utc).isoformat(timespec="seconds")}

def main():
 p=argparse.ArgumentParser(); p.add_argument("profile",choices=sorted(PROFILES)); p.add_argument("mission"); p.add_argument("report",type=Path); p.add_argument("--actor",required=True); a=p.parse_args()
 print(json.dumps(apply_oracle_pass(a.profile,a.mission,a.report,a.actor),sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
