import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
MODULE=ROOT/"overlay/scripts/station_interagent_broker.py"

def load():
 spec=importlib.util.spec_from_file_location("smart_broker",MODULE); assert spec and spec.loader
 module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

class Pool:
 def __init__(self): self.calls=[]
 def submit(self,fn): self.calls.append(fn)

def test_note_is_quiet_and_never_queues_thread(tmp_path,monkeypatch):
 m=load(); pool=Pool(); monkeypatch.setattr(m,"_WORK_POOL",pool); monkeypatch.setattr(m,"WORK_DISPATCHER",tmp_path/"dispatcher.py"); m.WORK_DISPATCHER.write_text("ok")
 store=m.MessageStore(tmp_path/"broker.db"); record=store.send("mission","agentik","FYI only",mode="note")
 assert m.queue_interagent_work(record) is False
 assert pool.calls==[] and record["mode"]=="note"

def test_delegate_queues_cross_agent_mission(tmp_path,monkeypatch):
 m=load(); pool=Pool(); monkeypatch.setattr(m,"_WORK_POOL",pool); monkeypatch.setattr(m,"WORK_DISPATCHER",tmp_path/"dispatcher.py"); m.WORK_DISPATCHER.write_text("ok")
 store=m.MessageStore(tmp_path/"broker.db"); record=store.send("mission","agentik","Own this bounded mission",mode="delegate")
 assert m.queue_interagent_work(record) is True
 assert len(pool.calls)==1 and record["mode"]=="delegate"

def test_self_send_is_forbidden_for_all_modes(tmp_path):
 m=load(); store=m.MessageStore(tmp_path/"broker.db")
 for mode in ("note","delegate"):
  with pytest.raises(m.BrokerError,match="self-directed"): store.send("mission","mission","continue my plan",mode=mode)

def test_unknown_mode_is_forbidden(tmp_path):
 m=load(); store=m.MessageStore(tmp_path/"broker.db")
 with pytest.raises(m.BrokerError,match="note or delegate"): store.send("mission","agentik","work",mode="thread")
