"""Automatic prompt archive and model-facing AGK completion ledger tool."""
from __future__ import annotations
import importlib.util, json, os
from pathlib import Path
from typing import Any

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


def _harness_path():
 root=Path(os.environ.get("AGK_TERMINAL_ROOT","/usr/local/lib/agk-terminal"))
 return root/"scripts"/"completion_harness.py"

def _module():
 path=_harness_path(); spec=importlib.util.spec_from_file_location("agk_completion_runtime",path)
 if spec is None or spec.loader is None: raise RuntimeError("completion harness unavailable")
 module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

def open_store():
 home=Path(os.environ.get("HERMES_HOME") or Path.home()/".hermes")
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
 profile=Path(os.environ.get("HERMES_HOME") or Path.home()/".hermes").name
 store=open_store()
 try:
  store.archive_prompt(text,source=platform,session_id=session_id,profile=profile,source_key=f"pre_llm:{session_id}:{turn_id}")
 finally: store.close()

def completion_available(): return _harness_path().is_file()

async def handle_completion(args:dict):
 from tools.registry import tool_error,tool_result
 try:
  action=str(args.get("action") or ""); store=open_store()
  try:
   if action=="archive":
    value=store.archive_prompt(str(args.get("text") or ""),source=str(args.get("source") or "agent"),session_id=str(args.get("session_id") or "unknown"),profile=str(args.get("profile") or "default")); return tool_result({"success":True,"prompt_id":value})
   if action=="create_mission":
    value=store.create_mission(str(args.get("mission_id") or "") or None,[str(args.get("prompt_id") or "")]); return tool_result({"success":True,"mission_id":value})
   if action=="add_requirement":
    value=store.add_requirement(str(args.get("prompt_id") or ""),str(args.get("text") or ""),mission_id=str(args.get("mission_id") or "") or None,human_gate=bool(args.get("human_gate"))); return tool_result({"success":True,"requirement_id":value})
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

def completion_prompt():
 return ("AGK Completion Harness is active. The original user prompt is archived before execution. For every non-trivial request, use agk_completion to create a mission and persist every explicit/implicit requirement, constraint, deliverable, approval gate and committed follow-up. Attach artifacts and PASS evidence per requirement. Before saying done, call gate; continue the Gauntlet/Loop-Graph until permit_done=true. Never start newly discovered backlog work without explicit human authorization.")
