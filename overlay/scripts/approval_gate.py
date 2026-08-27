#!/usr/bin/env python3
"""Root-owned human approval gate for exact Station requirements."""
from __future__ import annotations
import argparse, importlib.util, json, os, pwd
from pathlib import Path

HARNESS_PATH=Path("/usr/local/lib/agk-terminal/scripts/completion_harness.py")
PROFILES={"operator":("operator","/home/operator/.hermes"),"agentik":("agentik","/home/agentik/.hermes"),"mission":("mission","/home/mission/.hermes"),"private":("private","/home/private/.hermes"),"collective":("mission","/home/mission/.hermes/profiles/collective"),"nutrition-os":("operator","/home/operator/.hermes/profiles/nutrition-os")}

def _module():
 spec=importlib.util.spec_from_file_location("station_approval_harness",HARNESS_PATH)
 if spec is None or spec.loader is None: raise RuntimeError("completion harness unavailable")
 module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

def approve(profile,mission,requirement,actor,source,scope):
 if os.geteuid()!=0: raise PermissionError("approval gate requires root")
 if profile not in PROFILES or source not in {"discord","local-owner"}: raise ValueError("invalid approval target/source")
 user,home=PROFILES[profile]; account=pwd.getpwnam(user); module=_module(); store=module.CompletionStore(Path(home)/"completion",profile=profile)
 try:
  row=store.get_requirement(requirement)
  if row.get("mission_id")!=mission or not row.get("human_gate"): raise ValueError("requirement is not the requested human gate")
  auth=store.record_authorization(mission,requirement,actor=actor,source=source,scope=scope)
 finally:
  store.close()
  for path in (Path(home)/"completion").rglob("*"):
   if path.is_file(): os.chown(path,account.pw_uid,account.pw_gid)
 return {"profile":profile,"mission_id":mission,"requirement_id":requirement,"authorization_id":auth,"actor":actor,"source":source}

def main():
 p=argparse.ArgumentParser(); p.add_argument("profile",choices=sorted(PROFILES)); p.add_argument("mission"); p.add_argument("requirement"); p.add_argument("--actor",required=True); p.add_argument("--source",required=True,choices=("discord","local-owner")); p.add_argument("--scope",required=True); a=p.parse_args(); print(json.dumps(approve(a.profile,a.mission,a.requirement,a.actor,a.source,a.scope),sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
