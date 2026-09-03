"""Persistent, editable Station action messages for Discord plans."""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


_ACTIVE_STATES=frozenset({"RUNNING","VERIFYING","BLOCKED"})
_RESOLVED_ITEM_STATES=frozenset({"completed","cancelled"})
_STATE_LOCK=threading.RLock()
_CHECKLIST_MARKS={"completed":"✓","pending":"·","cancelled":"—"}
_ACTION_MESSAGE_LIMIT=1900


def _definitive_missing_message(result:Any)->bool:
 error=str(getattr(result,"error","") or "").lower()
 return "10008" in error


def _compact(value: Any, limit: int) -> str:
 text=re.sub(r"\s+"," ",str(value or "")).strip()
 if len(text)<=limit: return text
 return text[:limit-3].rstrip()+"..."


def normalize_plan(items: Iterable[dict]) -> list[dict[str,str]]:
 rows=[]
 for raw in items or []:
  if not isinstance(raw,dict): continue
  item_id=_compact(raw.get("id"),80)
  content=_compact(raw.get("content"),180)
  status=str(raw.get("status") or "pending").strip().lower()
  if not item_id or not content or status not in {"pending","in_progress","completed","cancelled"}: continue
  rows.append({"id":item_id,"content":content,"status":status})
 return rows


def plan_metrics(items: Iterable[dict]) -> dict[str,Any]:
 rows=normalize_plan(items); total=len(rows)
 completed=sum(row["status"] in _RESOLVED_ITEM_STATES for row in rows)
 percent=round(completed*100/total) if total else 0
 filled=round(percent/10)
 current=next((row["content"] for row in rows if row["status"]=="in_progress"),None)
 if current is None: current=next((row["content"] for row in rows if row["status"]=="pending"),None)
 status="COMPLETE" if total and completed==total else "RUNNING"
 return {"rows":rows,"total":total,"completed":completed,"percent":percent,
         "bar":"█"*filled+"░"*(10-filled),"current":current or "Final verification complete","status":status}


def _active_mark(state:str)->str:
 state=str(state or "RUNNING").upper().replace("_"," ")
 if state=="VERIFYING": return "◇"
 if state in {"BLOCKED","WAITING"}: return "‖"
 if state=="FAILED": return "×"
 if state=="CANCELLED": return "—"
 if state=="COMPLETE": return "✓"
 if "DECISION" in state or "ATTENTION" in state: return "!"
 return "→"


def render_action_message(label: str, objective: str, items: Iterable[dict], *, status: str|None=None, blocked_reason: str|None=None) -> str:
 metrics=plan_metrics(items); state=str(status or metrics["status"]).upper()
 title=_compact(label or "Station",32).upper()
 objective=_compact(objective,180) or "Executing requested action"
 active_mark=_active_mark(state)
 lines=[f"{title} / {state}","",objective,"",f"{metrics['completed']} of {metrics['total']} actions resolved",f"{metrics['bar']} {metrics['percent']}%","","NOW",f"{active_mark} {metrics['current']}"]
 if str(state).upper()=="BLOCKED":
  reason=_compact(blocked_reason,180)
  if reason: lines.extend(["","BLOCKED REASON",reason])
 base="\n".join(lines)
 rows=metrics["rows"]
 if not rows: return base
 prefix=base+"\n\nPLAN\n"
 available=max(0,_ACTION_MESSAGE_LIMIT-len(prefix)-len(rows))
 item_limit=max(8,min(180,available//len(rows)-2))
 checklist=[f"{active_mark if row['status']=='in_progress' else _CHECKLIST_MARKS[row['status']]} {_compact(row['content'],item_limit)}" for row in rows]
 return prefix+"\n".join(checklist)


def parse_todo_result(result: Any) -> list[dict[str,str]]:
 if isinstance(result,dict): payload=result
 else:
  text=str(result or "").strip()
  try: payload=json.loads(text)
  except (TypeError,ValueError,json.JSONDecodeError): return []
 if not isinstance(payload,dict): return []
 # Registry tool envelopes may wrap the JSON payload under result/output.
 for key in ("result","output"):
  nested=payload.get(key)
  if isinstance(nested,str):
   try:
    candidate=json.loads(nested)
    if isinstance(candidate,dict): payload=candidate
   except (TypeError,ValueError,json.JSONDecodeError): pass
 items=payload.get("todos")
 return normalize_plan(items if isinstance(items,list) else [])


class StationActionMessage:
 def __init__(self,*,adapter:Any,state_path:Path|str,session_key:str,chat_id:str,
              thread_id:str|None,objective:str,label:str="Operator",reply_to:str|None=None):
  self.adapter=adapter; self.state_path=Path(state_path); self.session_key=str(session_key)
  self.chat_id=str(chat_id); self.thread_id=str(thread_id) if thread_id else None
  self.objective=_compact(objective,180); self.label=_compact(label,32) or "Operator"
  self.reply_to=str(reply_to) if reply_to else None; self.items:list[dict[str,str]]=[]
  self.message_id:str|None=None; self.status="RUNNING"
  self.action_id=uuid.uuid4().hex; self.revision=0; self.blocked_reason=""
  self._operation_lock=asyncio.Lock()
  state=self._load().get(self.session_key)
  if isinstance(state,dict) and str(state.get("status")) in _ACTIVE_STATES:
   if str(state.get("chat_id"))==self.chat_id and str(state.get("thread_id") or "")==str(self.thread_id or ""):
    self.message_id=str(state.get("message_id") or "") or None
    self.objective=_compact(state.get("objective") or self.objective,180)
    self.label=_compact(state.get("label") or self.label,32)
    self.status=str(state.get("status") or "RUNNING")
    self.items=normalize_plan(state.get("items") or [])
    self.action_id=str(state.get("action_id") or self.action_id)
    self.revision=max(0,int(state.get("revision") or 0))
    self.blocked_reason=_compact(state.get("blocked_reason") or "",180)

 def _load(self)->dict:
  with _STATE_LOCK:
   try:
    data=json.loads(self.state_path.read_text(encoding="utf-8"))
    return data if isinstance(data,dict) else {}
   except (OSError,ValueError,TypeError,json.JSONDecodeError): return {}

 def _persist(self)->None:
  with _STATE_LOCK:
   data=self._load(); data[self.session_key]={
    "message_id":self.message_id,"chat_id":self.chat_id,"thread_id":self.thread_id,
    "objective":self.objective,"label":self.label,"status":self.status,
    "items":self.items,"action_id":self.action_id,"revision":self.revision,
    "blocked_reason":self.blocked_reason,
    "updated_at":int(time.time()),
   }
   self.state_path.parent.mkdir(parents=True,exist_ok=True)
   fd,tmp=tempfile.mkstemp(prefix=f".{self.state_path.name}.",dir=str(self.state_path.parent))
   try:
    payload=data[self.session_key]
    if str(payload.get("status") or "").upper()=="BLOCKED" and not str(payload.get("blocked_reason") or "").strip():
     payload["blocked_reason"]="turn ended with unresolved plan items; Station is not a completion gate"
    with os.fdopen(fd,"w",encoding="utf-8") as handle:
     json.dump(data,handle,ensure_ascii=False,indent=2,sort_keys=True); handle.write("\n")
    os.chmod(tmp,0o600); os.replace(tmp,self.state_path)
   finally:
    try: os.unlink(tmp)
    except FileNotFoundError: pass

 def _metadata(self)->dict|None:
  return {"thread_id":self.thread_id} if self.thread_id else None

 async def apply_plan(self,items:Iterable[dict])->bool:
  async with self._operation_lock:
   return await self._apply_plan(items)

 async def _apply_plan(self,items:Iterable[dict])->bool:
  rows=normalize_plan(items)
  if not rows: return False
  self.items=rows; self.status=plan_metrics(rows)["status"]
  content=render_action_message(self.label,self.objective,rows,status=self.status)
  if self.message_id:
   result=await self.adapter.edit_message(chat_id=self.chat_id,message_id=self.message_id,content=content,metadata=self._metadata())
   if getattr(result,"success",False): self.revision+=1; self._persist(); return True
   if not _definitive_missing_message(result): return False
   self.message_id=None
  result=await self.adapter.send(chat_id=self.chat_id,content=content,reply_to=self.reply_to,metadata=self._metadata())
  if not getattr(result,"success",False) or not getattr(result,"message_id",None): return False
  self.message_id=str(result.message_id); self.revision+=1; self._persist(); return True

 async def finish(self,*,failed:bool=False,cancelled:bool=False,blocked_reason:str|None=None)->bool:
  async with self._operation_lock:
   return await self._finish(failed=failed,cancelled=cancelled,blocked_reason=blocked_reason)

 async def _finish(self,*,failed:bool=False,cancelled:bool=False,blocked_reason:str|None=None)->bool:
  if not self.message_id or not self.items: return False
  current=self._load().get(self.session_key)
  if isinstance(current,dict) and str(current.get("action_id") or "")==self.action_id:
   if int(current.get("revision") or 0)>self.revision: return False
  metrics=plan_metrics(self.items)
  if cancelled: status="CANCELLED"
  elif failed: status="FAILED"
  elif metrics["status"]=="COMPLETE": status="COMPLETE"
  else: status="WAITING"
  self.status=status
  if status=="WAITING":
   reason=_compact(blocked_reason or self.blocked_reason,180)
   if not reason:
    reason="background work still running; Station keeps this card live"
   self.blocked_reason=reason
  content=render_action_message(self.label,self.objective,self.items,status=status,blocked_reason=self.blocked_reason)
  result=await self.adapter.edit_message(chat_id=self.chat_id,message_id=self.message_id,content=content,metadata=self._metadata())
  if getattr(result,"success",False): self.revision+=1; self._persist(); return True
  return False
