import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


ROOT=Path(__file__).resolve().parents[1]
MODULE=ROOT/"overlay/hermes-core/gateway/station_action_message.py"


def load():
 spec=importlib.util.spec_from_file_location("station_action_message_test",MODULE)
 assert spec and spec.loader
 module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


class FakeAdapter:
 def __init__(self): self.sent=[]; self.edited=[]
 async def send(self,chat_id,content,reply_to=None,metadata=None):
  self.sent.append({"chat_id":chat_id,"content":content,"reply_to":reply_to,"metadata":metadata})
  return SimpleNamespace(success=True,message_id="message-1")
 async def edit_message(self,chat_id,message_id,content,metadata=None):
  self.edited.append({"chat_id":chat_id,"message_id":message_id,"content":content,"metadata":metadata})
  return SimpleNamespace(success=True,message_id=message_id)


def todos(active="build",completed=0):
 rows=[
  {"id":"plan","content":"Create the plan","status":"completed"},
  {"id":"build","content":"Build the action message","status":"in_progress"},
  {"id":"verify","content":"Verify runtime continuity","status":"pending"},
 ]
 if completed>=2:
  rows[1]["status"]="completed"; rows[2]["status"]="in_progress"
 if completed>=3:
  rows[1]["status"]="completed"; rows[2]["status"]="completed"
 return rows


def test_first_plan_sends_one_action_message_then_updates_edit_in_place(tmp_path):
 module=load(); adapter=FakeAdapter()
 consumer=module.StationActionMessage(
  adapter=adapter,state_path=tmp_path/"actions.json",session_key="discord:guild:thread",
  chat_id="123",thread_id="456",objective="Build quieter Discord notifications",
 )
 asyncio.run(consumer.apply_plan(todos()))
 first=adapter.sent[0]["content"]
 assert "PLAN\n✓ Create the plan\n→ Build the action message\n· Verify runtime continuity" in first
 asyncio.run(consumer.apply_plan(todos(completed=2)))
 assert len(adapter.sent)==1
 assert len(adapter.edited)==1
 assert adapter.edited[0]["message_id"]=="message-1"
 assert "███████░░░" in adapter.edited[0]["content"]
 assert "2 of 3 actions resolved" in adapter.edited[0]["content"]


def test_telegram_plan_uses_one_message_and_edits_it_in_place(tmp_path):
 module=load(); adapter=FakeAdapter()
 consumer=module.StationActionMessage(
  adapter=adapter,state_path=tmp_path/"actions.json",session_key="telegram:mission:topic",
  chat_id="mission-chat",thread_id="topic-42",objective="Onboard the Mission project",
 )
 asyncio.run(consumer.apply_plan(todos()))
 asyncio.run(consumer.apply_plan(todos(completed=2)))
 assert len(adapter.sent)==1
 assert len(adapter.edited)==1
 assert adapter.sent[0]["metadata"]=={"thread_id":"topic-42"}
 assert adapter.edited[0]["metadata"]=={"thread_id":"topic-42"}
 assert "PLAN\n✓ Create the plan" in adapter.sent[0]["content"]


def test_restart_resumes_same_running_action_message(tmp_path):
 module=load(); first=FakeAdapter()
 consumer=module.StationActionMessage(
  adapter=first,state_path=tmp_path/"actions.json",session_key="same-session",
  chat_id="123",thread_id="456",objective="Persistent action",
 )
 asyncio.run(consumer.apply_plan(todos()))
 second=FakeAdapter()
 resumed=module.StationActionMessage(
  adapter=second,state_path=tmp_path/"actions.json",session_key="same-session",
  chat_id="123",thread_id="456",objective="Persistent action",
 )
 asyncio.run(resumed.apply_plan(todos(completed=2)))
 assert second.sent==[]
 assert len(second.edited)==1
 assert second.edited[0]["message_id"]=="message-1"


def test_terminal_plan_state_edits_action_card_but_never_sends_final_reply(tmp_path):
 module=load(); adapter=FakeAdapter()
 consumer=module.StationActionMessage(
  adapter=adapter,state_path=tmp_path/"actions.json",session_key="terminal",
  chat_id="123",thread_id=None,objective="Finish action",
 )
 asyncio.run(consumer.apply_plan(todos()))
 asyncio.run(consumer.apply_plan(todos(completed=3)))
 assert len(adapter.sent)==1
 assert len(adapter.edited)==1
 assert "COMPLETE" in adapter.edited[0]["content"]
 state=json.loads((tmp_path/"actions.json").read_text())
 assert state["terminal"]["status"]=="COMPLETE"


def test_new_action_after_completed_state_sends_a_new_message(tmp_path):
 module=load(); first=FakeAdapter()
 consumer=module.StationActionMessage(
  adapter=first,state_path=tmp_path/"actions.json",session_key="repeat",
  chat_id="123",thread_id=None,objective="First action",
 )
 asyncio.run(consumer.apply_plan(todos(completed=3)))
 second=FakeAdapter()
 next_action=module.StationActionMessage(
  adapter=second,state_path=tmp_path/"actions.json",session_key="repeat",
  chat_id="123",thread_id=None,objective="Second action",
 )
 asyncio.run(next_action.apply_plan(todos()))
 assert len(second.sent)==1
 assert second.edited==[]


def test_render_is_monochrome_compact_and_uses_canonical_plan_only():
 module=load(); text=module.render_action_message("Operator","Do the work",todos(completed=2))
 assert text.startswith("OPERATOR / RUNNING")
 assert "2 of 3 actions resolved\n███████░░░ 67%" in text
 assert "NOW\n→ Verify runtime continuity" in text
 assert "🟡" not in text and "🟢" not in text


def test_render_uses_editorial_minimal_live_checklist():
 module=load(); rows=[
  {"id":"done","content":"Inspect the current runtime","status":"completed"},
  {"id":"active","content":"Deploy the shared renderer","status":"in_progress"},
  {"id":"next","content":"Reload every gateway","status":"pending"},
  {"id":"skip","content":"Discard obsolete fallback","status":"cancelled"},
 ]
 text=module.render_action_message("Operator","Expose the live plan",rows)
 assert "2 of 4 actions resolved" in text
 assert "\nNOW\n→ Deploy the shared renderer" in text
 assert "\nPLAN\n" in text
 assert "✓ Inspect the current runtime" in text
 assert "→ Deploy the shared renderer" in text
 assert "· Reload every gateway" in text
 assert "— Discard obsolete fallback" in text
 plan=text.split("\nPLAN\n",1)[1]
 assert plan.index("Inspect the current runtime") < plan.index("Deploy the shared renderer")
 assert plan.index("Deploy the shared renderer") < plan.index("Reload every gateway")


def test_long_plan_keeps_every_action_visible_inside_one_discord_message():
 module=load(); rows=[
  {"id":f"step-{index}","content":f"Action {index:02d} "+("with detailed operational context "*8),"status":"pending"}
  for index in range(1,25)
 ]
 text=module.render_action_message("Operator","Execute a long canonical plan",rows)
 assert len(text)<=1900
 for index in range(1,25):
  assert f"Action {index:02d}" in text
 assert "more" not in text.lower()


def test_editorial_active_mark_reflects_terminal_state():
 module=load(); rows=[{"id":"active","content":"Inspect runtime evidence","status":"in_progress"}]
 assert "◇ Inspect runtime evidence" in module.render_action_message("Operator","Audit",rows,status="VERIFYING")
 assert "‖ Inspect runtime evidence" in module.render_action_message("Operator","Audit",rows,status="WAITING")
 assert "‖ Inspect runtime evidence" in module.render_action_message("Operator","Audit",rows,status="BLOCKED")
 assert "× Inspect runtime evidence" in module.render_action_message("Operator","Audit",rows,status="FAILED")


def test_concurrent_plan_updates_cannot_create_duplicate_action_messages(tmp_path):
 module=load()
 class SlowAdapter(FakeAdapter):
  async def send(self,*args,**kwargs):
   await asyncio.sleep(0.02)
   return await super().send(*args,**kwargs)
 adapter=SlowAdapter()
 consumer=module.StationActionMessage(
  adapter=adapter,state_path=tmp_path/"actions.json",session_key="concurrent",
  chat_id="123",thread_id=None,objective="Concurrent updates",
 )
 async def run():
  await asyncio.gather(consumer.apply_plan(todos()),consumer.apply_plan(todos(completed=2)))
 asyncio.run(run())
 assert len(adapter.sent)==1
 assert len(adapter.edited)==1


def test_transient_edit_failure_never_creates_replacement_message(tmp_path):
 module=load()
 class TransientAdapter(FakeAdapter):
  async def edit_message(self,chat_id,message_id,content,metadata=None):
   self.edited.append({"message_id":message_id,"content":content})
   return SimpleNamespace(success=False,message_id=message_id,error="ConnectTimeout",retryable=True)
 adapter=TransientAdapter(); consumer=module.StationActionMessage(
  adapter=adapter,state_path=tmp_path/"actions.json",session_key="transient",
  chat_id="123",thread_id=None,objective="Transient edit",
 )
 asyncio.run(consumer.apply_plan(todos()))
 asyncio.run(consumer.apply_plan(todos(completed=2)))
 assert len(adapter.sent)==1
 assert consumer.message_id=="message-1"


def test_definitive_missing_message_allows_one_replacement(tmp_path):
 module=load()
 assert module._definitive_missing_message(SimpleNamespace(error="Discord error code: 10008 Unknown Message")) is True
 assert module._definitive_missing_message(SimpleNamespace(error="message not found after proxy timeout")) is False
 assert module._definitive_missing_message(SimpleNamespace(error="unknown message from upstream gateway")) is False
 class MissingAdapter(FakeAdapter):
  async def edit_message(self,chat_id,message_id,content,metadata=None):
   self.edited.append({"message_id":message_id,"content":content})
   return SimpleNamespace(success=False,message_id=message_id,error="Discord error code: 10008 Unknown Message",retryable=False)
 adapter=MissingAdapter(); consumer=module.StationActionMessage(
  adapter=adapter,state_path=tmp_path/"actions.json",session_key="missing",
  chat_id="123",thread_id=None,objective="Missing edit",
 )
 asyncio.run(consumer.apply_plan(todos()))
 asyncio.run(consumer.apply_plan(todos(completed=2)))
 assert len(adapter.sent)==2


def test_stale_outer_turn_cannot_overwrite_newer_followup_action_state(tmp_path):
 module=load(); adapter=FakeAdapter()
 outer=module.StationActionMessage(
  adapter=adapter,state_path=tmp_path/"actions.json",session_key="followup",
  chat_id="123",thread_id=None,objective="Queued work",
 )
 asyncio.run(outer.apply_plan(todos()))
 inner=module.StationActionMessage(
  adapter=adapter,state_path=tmp_path/"actions.json",session_key="followup",
  chat_id="123",thread_id=None,objective="Queued work",
 )
 asyncio.run(inner.apply_plan(todos(completed=3)))
 edits_before=len(adapter.edited)
 assert asyncio.run(outer.finish()) is False
 assert len(adapter.edited)==edits_before
 state=json.loads((tmp_path/"actions.json").read_text())["followup"]
 assert state["status"]=="COMPLETE"
