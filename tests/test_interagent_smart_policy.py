import gc
import importlib.util
import os
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
 assert m.queue_interagent_work(record,store=store) is True
 assert len(pool.calls)==1 and record["mode"]=="delegate"

def test_self_send_is_forbidden_for_all_modes(tmp_path):
 m=load(); store=m.MessageStore(tmp_path/"broker.db")
 for mode in ("note","delegate"):
  with pytest.raises(m.BrokerError,match="self-directed"): store.send("mission","mission","continue my plan",mode=mode)

def test_unknown_mode_is_forbidden(tmp_path):
 m=load(); store=m.MessageStore(tmp_path/"broker.db")
 with pytest.raises(m.BrokerError,match="note or delegate"): store.send("mission","agentik","work",mode="thread")

def test_duplicate_pending_delegate_is_not_queued_twice_and_failure_becomes_retryable(tmp_path,monkeypatch):
 m=load(); pool=Pool(); monkeypatch.setattr(m,"_WORK_POOL",pool); monkeypatch.setattr(m,"WORK_DISPATCHER",tmp_path/"dispatcher.py"); m.WORK_DISPATCHER.write_text("ok")
 store=m.MessageStore(tmp_path/"broker.db"); record=store.send("mission","agentik","Own this bounded mission",mode="delegate")
 monkeypatch.setattr(m,"process_interagent_work",lambda *_args,**_kwargs: False)
 assert m.queue_interagent_work(record,store=store) is True
 assert m.queue_interagent_work(record,store=store) is False
 assert len(pool.calls)==1
 pool.calls[0]()
 assert m.queue_interagent_work(record,store=store) is True

def test_recovery_loop_retries_pending_work_without_restart():
 m=load(); calls=[]
 class Broker:
  def recover_pending(self): calls.append("tick")
 class Stop:
  def __init__(self): self.count=0
  def wait(self,_interval): self.count+=1; return self.count>2
 m.recover_pending_forever(Broker(),Stop(),interval=0)
 assert calls==["tick","tick"]

def test_message_store_closes_connections_during_repeated_recovery_reads(tmp_path):
 m=load(); db_path=tmp_path/"broker.db"; store=m.MessageStore(db_path)
 def db_fds():
  total=0
  for fd in Path("/proc/self/fd").iterdir():
   try:
    if os.readlink(fd)==str(db_path): total+=1
   except OSError:
    pass
  return total
 before=db_fds(); gc.disable()
 try:
  for _ in range(100): store.pending_delegates()
  assert db_fds() <= before+1
 finally:
  gc.enable(); gc.collect()
