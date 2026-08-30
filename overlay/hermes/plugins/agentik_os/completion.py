"""Automatic prompt archive and model-facing AGK completion ledger tool."""
from __future__ import annotations
import importlib.util, json, os, threading
from collections import OrderedDict
from pathlib import Path
from typing import Any
from hermes_constants import get_hermes_home

_COMPLETION_SCHEMA={
 "name":"agk_completion","description":"Persist and verify AGK prompt/requirement/artifact/evidence graphs.",
 "parameters":{"type":"object","properties":{
   "action":{"type":"string","enum":["archive","create_mission","add_requirement","set_status","artifact","evidence","gate","finding"]},
   "prompt_id":{"type":"string"},"mission_id":{"type":"string"},"requirement_id":{"type":"string"},
   "text":{"type":"string"},"status":{"type":"string"},"source":{"type":"string"},"session_id":{"type":"string"},
   "profile":{"type":"string"},"type":{"type":"string"},"location":{"type":"string"},"result":{"type":"string"},
   "reference":{"type":"string"},"classification":{"type":"string"},"severity":{"type":"string"},
   "human_gate":{"type":"boolean"},"requirement_ids":{"type":"array","items":{"type":"string"}}
 },"required":["action"]}}
AGK_COMPLETION_TOOL_SCHEMA=_COMPLETION_SCHEMA

_PLAN_EXEMPT_TOOLS=frozenset({
 "todo","clarify","skill_view","skills_list","session_search",
 # Read-only discovery must remain available so an agent can build a complete,
 # evidence-based plan instead of guessing or looping on the plan gate.
 "tool_search","tool_describe","read_file","search_files","web_search","web_extract",
})
_PLANNED_TURNS: OrderedDict[tuple[str, str, str], None] = OrderedDict()
_PLAN_LOCK=threading.Lock()
_PLAN_TURN_LIMIT=2048


def _plan_key(session_id:str,turn_id:str,task_id:str="")->tuple[str,str,str]|None:
 session=str(session_id or "").strip()
 # Hermes' cron loop can rotate turn_id between tool cycles while task_id stays
 # stable for the complete scheduled run. Prefer task_id when present; retain
 # turn_id as the backward-compatible boundary for runtimes that omit task_id.
 scope=str(task_id or "").strip() or str(turn_id or "").strip()
 if not session or not scope: return None
 try: home=str(get_hermes_home().resolve())
 except Exception: return None  # noqa: BLE001 - policy gate must fail closed
 return home,session,scope


def require_plan_before_work(tool_name:str="",args:dict|None=None,session_id:str="",turn_id:str="",task_id:str="",**_kwargs):
 """Block operational tools until the current task/turn has applied a todo plan."""
 name=str(tool_name or "").strip()
 if name=="todo" or name in _PLAN_EXEMPT_TOOLS:
  return None
 key=_plan_key(session_id,turn_id,task_id)
 if key is None:
  return {"action":"block","message":"Plan Mode required: missing session/task/turn identity; apply a canonical todo plan before operational work."}
 with _PLAN_LOCK: planned=key in _PLANNED_TURNS
 if planned:
  return None
 return {"action":"block","message":"Plan Mode required: apply a canonical todo plan before operational work, then retry this tool."}


def _canonical_plan_from_result(result:Any)->list[dict]:
 payload=result if isinstance(result,dict) else None
 if payload is None:
  try: payload=json.loads(str(result or ""))
  except (TypeError,ValueError,json.JSONDecodeError): return []
 if not isinstance(payload,dict): return []
 for key in ("result","output"):
  nested=payload.get(key)
  if isinstance(nested,str):
   try:
    decoded=json.loads(nested)
    if isinstance(decoded,dict): payload=decoded
   except (TypeError,ValueError,json.JSONDecodeError): pass
 rows=payload.get("todos")
 if not isinstance(rows,list) or not rows: return []
 valid=[]; ids=set()
 for row in rows:
  if not isinstance(row,dict): return []
  item_id=str(row.get("id") or "").strip(); content=str(row.get("content") or "").strip()
  status=str(row.get("status") or "").strip().lower()
  if not item_id or not content or item_id in ids or status not in {"pending","in_progress","completed","cancelled"}: return []
  ids.add(item_id); valid.append({"id":item_id,"content":content,"status":status})
 unresolved=[row for row in valid if row["status"] not in {"completed","cancelled"}]
 if not unresolved or sum(row["status"]=="in_progress" for row in unresolved)!=1: return []
 return valid


def record_applied_plan(tool_name:str="",args:dict|None=None,session_id:str="",turn_id:str="",task_id:str="",result:Any=None,status:str="",**_kwargs):
 """Authorize work only after a successful canonical todo mutation is persisted."""
 if str(tool_name or "").strip()!="todo" or str(status or "").lower() not in {"ok","success"}: return None
 submitted=args.get("todos") if isinstance(args,dict) else None
 if not isinstance(submitted,list) or not submitted: return None
 key=_plan_key(session_id,turn_id,task_id)
 if key is None or not _canonical_plan_from_result(result): return None
 with _PLAN_LOCK:
  _PLANNED_TURNS[key]=None; _PLANNED_TURNS.move_to_end(key)
  while len(_PLANNED_TURNS)>_PLAN_TURN_LIMIT: _PLANNED_TURNS.popitem(last=False)
 return None


def _harness_path():
 root=Path(os.environ.get("AGK_TERMINAL_ROOT","/usr/local/lib/agk-terminal"))
 return root/"scripts"/"completion_harness.py"

def _module():
 path=_harness_path(); spec=importlib.util.spec_from_file_location("agk_completion_runtime",path)
 if spec is None or spec.loader is None: raise RuntimeError("completion harness unavailable")
 module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

def open_store():
 home=get_hermes_home()
 return _module().CompletionStore(home/"completion")

def _message_text(value:Any)->str:
 if isinstance(value,str): return value
 if isinstance(value,dict):
  content=value.get("content") or value.get("text") or ""
  return content if isinstance(content,str) else json.dumps(content,ensure_ascii=False)
 if isinstance(value,list): return "\n".join(filter(None,(_message_text(item) for item in value)))
 return ""

def archive_before_execution(**kwargs):
 text=_message_text(kwargs.get("user_message"))
 if not text.strip(): return
 session_id=str(kwargs.get("session_id") or "unknown")
 turn_id=str(kwargs.get("turn_id") or _module().sha256_text(text)[:16])
 platform=str(kwargs.get("platform") or "hermes")
 home=get_hermes_home(); profile=home.name if home.name!=".hermes" else Path.home().name
 store=open_store()
 try:
  store.archive_prompt(text,source=platform,session_id=session_id,profile=profile,source_key=f"pre_llm:{session_id}:{turn_id}")
 finally: store.close()

def completion_available(): return _harness_path().is_file()

async def handle_completion(args:dict, **_kwargs):
 from tools.registry import tool_error,tool_result
 action=str(args.get("action") or "")
 if action=="add_requirement" and not str(args.get("mission_id") or "").strip():
  return tool_error("mission_id is required for add_requirement")
 try:
  store=open_store()
  try:
   if action=="archive":
    value=store.archive_prompt(str(args.get("text") or ""),source=str(args.get("source") or "agent"),session_id=str(args.get("session_id") or "unknown"),profile=str(args.get("profile") or "default")); return tool_result({"success":True,"prompt_id":value})
   if action=="create_mission":
    value=store.create_mission(str(args.get("mission_id") or "") or None,[str(args.get("prompt_id") or "")]); return tool_result({"success":True,"mission_id":value})
   if action=="add_requirement":
    prompt_id=str(args.get("prompt_id") or "").strip()
    if not prompt_id:
     rows=store.db.execute("SELECT prompt_id FROM mission_prompts WHERE mission_id=? ORDER BY prompt_id",(str(args.get("mission_id")),)).fetchall()
     if len(rows)!=1:
      return tool_error("prompt_id is required when the mission does not have exactly one archived prompt")
     prompt_id=str(rows[0][0])
    value=store.add_requirement(prompt_id,str(args.get("text") or ""),mission_id=str(args.get("mission_id") or "") or None,human_gate=bool(args.get("human_gate"))); return tool_result({"success":True,"requirement_id":value})
   if action=="set_status": store.set_requirement_status(str(args.get("requirement_id") or ""),str(args.get("status") or "")); return tool_result({"success":True})
   if action=="artifact":
    value=store.add_artifact(str(args.get("mission_id") or ""),str(args.get("requirement_id") or "") or None,str(args.get("type") or "artifact"),str(args.get("location") or "")); return tool_result({"success":True,"artifact_id":value})
   if action=="evidence":
    verifier=str(args.get("type") or "agent")
    if verifier=="completion-oracle" or not args.get("requirement_id"):
     return tool_error("mission-level Completion Oracle evidence is reserved for the trusted Operator gate")
    value=store.add_evidence(str(args.get("mission_id") or ""),str(args.get("requirement_id") or ""),verifier,str(args.get("result") or "PASS"),str(args.get("reference") or "")); return tool_result({"success":True,"evidence_id":value})
   if action=="gate": return tool_result(store.completion_gate(str(args.get("mission_id") or "")))
   if action=="finding":
    value=store.create_finding(str(args.get("mission_id") or ""),str(args.get("classification") or "INCOMPLETE"),str(args.get("severity") or "P2"),list(args.get("requirement_ids") or [])); return tool_result({"success":True,"finding_id":value})
   return tool_error("unknown completion action")
  finally: store.close()
 except Exception as exc: return tool_error(f"AGK completion operation failed safely: {type(exc).__name__}")

def completion_prompt(_session_info: dict | None = None):
 return ("AGK Completion Harness is active. The original user prompt is archived before execution. For every non-trivial request, apply a canonical todo plan before operational work: enumerate every currently known action upfront, including pending verification, deployment, evidence, approval and rollback steps. The todo must publish the complete user-visible Station plan before operational work. Then use agk_completion to create a mission and persist every explicit/implicit requirement, constraint, deliverable, approval gate and committed follow-up. The Station messaging action message on Discord or Telegram is projected only from that canonical plan; its first visible version must expose the full known checklist, keep exactly one item in_progress, update the same message after verified transitions, and revise the visible plan when scope changes without emitting per-tool notifications. Attach artifacts and PASS evidence per requirement. Before saying done, call gate; continue the Gauntlet/Loop-Graph until permit_done=true. Add newly discovered work required by the current request to the canonical plan and continue autonomously when it is A0-A3 safe and reversible. Only unrelated backlog needs fresh human authorization; preserve genuine A4 owner gates and A5 forbidden boundaries.")
