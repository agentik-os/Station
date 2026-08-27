#!/usr/bin/env python3
"""Human-gated recovery decisions and cross-profile routing without state copying."""
from __future__ import annotations
import argparse, importlib.util, json, os, pwd, sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROFILES = {
    "operator": ("operator", "/home/operator/.hermes"),
    "agentik": ("agentik", "/home/agentik/.hermes"),
    "mission": ("mission", "/home/mission/.hermes"),
    "private": ("private", "/home/private/.hermes"),
    "collective": ("mission", "/home/mission/.hermes/profiles/collective"),
    "nutrition-os": ("operator", "/home/operator/.hermes/profiles/nutrition-os"),
}
DECISIONS = {"RELAUNCH", "BACKLOG", "IGNORE", "ALREADY_DONE"}


def resolve_finding(finding_id: str):
    if not finding_id.startswith("FIND-") or len(finding_id) > 40:
        raise ValueError("invalid finding id")
    for profile, (user, home) in PROFILES.items():
        db_path = Path(home) / "completion" / "completion.db"
        if not db_path.is_file(): continue
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try: row = db.execute("SELECT mission_id FROM findings WHERE id=?", (finding_id,)).fetchone()
        finally: db.close()
        if row: return profile, user, home, str(row[0])
    raise KeyError(finding_id)


def _chown_completion(home: str, account) -> None:
    root = Path(home) / "completion"
    for path in root.rglob("*"):
        if path.is_file():
            os.chown(path, account.pw_uid, account.pw_gid)


def _load_harness():
    path = Path("/usr/local/lib/agk-terminal/scripts/completion_harness.py")
    spec = importlib.util.spec_from_file_location("station_authorized_completion", path)
    if spec is None or spec.loader is None: raise RuntimeError("completion harness unavailable")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def decide(finding_id: str, decision: str, actor: str, source: str) -> dict:
    if os.geteuid()!=0: raise PermissionError("recovery routing requires root")
    decision=decision.upper(); actor=actor.strip(); source=source.strip()
    if decision not in DECISIONS or not actor or len(actor)>100 or source not in {"discord","local-owner"}:
        raise PermissionError("an explicit typed decision, bounded actor and trusted source are required")
    profile,user,home,mission_id=resolve_finding(finding_id)
    account=pwd.getpwnam(user); db_path=f"{home}/completion/completion.db"
    created_at=datetime.now(timezone.utc).isoformat(timespec="seconds")
    authorization={"id":f"ROOT-{finding_id}-{int(datetime.now(timezone.utc).timestamp())}","actor":actor,"source":source,
      "scope":f"relaunch:{finding_id}","timestamp":created_at,"authority":"root-recovery-router"}
    dispatch=Path("/var/lib/station/recovery/dispatch")
    payload={"schema":"agk.recovery.dispatch.v1","finding_id":finding_id,"mission_id":mission_id,
      "profile":profile,"decision":decision,"actor":actor,"source":source,"created_at":created_at,
      "authority":"root-recovery-router","instruction":"Route by profile/finding ID. Load original prompt only inside the owning profile boundary."}
    if decision=="RELAUNCH":
        module=_load_harness(); store=module.CompletionStore(Path(home)/"completion")
        try: store.relaunch_finding(finding_id,authorization=authorization)
        finally: store.close(); _chown_completion(home,account)
    else:
        db=sqlite3.connect(db_path)
        try: db.execute("UPDATE findings SET human_decision=?,updated_at=? WHERE id=?",(decision,created_at,finding_id)); db.commit()
        finally: db.close(); _chown_completion(home,account)
    dispatch.mkdir(parents=True,exist_ok=True)
    path=dispatch/f"{finding_id}.json"
    temporary=dispatch/f".{finding_id}.json.new"
    temporary.write_text(json.dumps(payload,indent=2)+"\n"); temporary.chmod(0o640)
    os.chown(temporary,0,pwd.getpwnam("operator").pw_gid); os.replace(temporary,path)
    return {**payload,"dispatch_path":str(path)}


def main():
    p=argparse.ArgumentParser(); p.add_argument("finding"); p.add_argument("decision",choices=sorted(DECISIONS)); p.add_argument("--actor",required=True); p.add_argument("--source",required=True,choices=("discord","local-owner")); a=p.parse_args()
    print(json.dumps(decide(a.finding,a.decision,a.actor,a.source),sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
