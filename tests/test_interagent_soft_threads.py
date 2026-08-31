import importlib.util
from pathlib import Path
import pytest

ROOT=Path(__file__).resolve().parents[1]
MODULE=ROOT/"overlay/scripts/station_interagent_work_dispatch.py"

def load():
 spec=importlib.util.spec_from_file_location("soft_threads",MODULE); assert spec and spec.loader
 module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module

class Discord:
 def __init__(self): self.created=[]; self.posts=[]; self.reused=[]; self.members=[]
 def create_thread(self,parent,name): self.created.append((parent,name)); return "thread-pair"
 def reuse_thread(self,thread): self.reused.append(thread); return thread
 def add_thread_member(self,thread,target): self.members.append((thread,target))
 def post_handoff(self,thread,content,target): self.posts.append((thread,content,target)); return f"message-{len(self.posts)}"
 def wait_for_bot_reply(self,*args): return f"reply-{len(self.posts)}"

def record(mid,body): return {"id":mid*32,"source":"mission","target":"agentik","mode":"delegate","body":body}

def test_agent_policy_uses_operator_only_for_root_boundaries():
 source=(ROOT/"overlay/hermes/plugins/agentik_os/interagent.py").read_text()
 assert "Operator only for root-owned" in source
 assert "must never call station_interagent back" in source
 assert "never create a self-thread" in source
 assert '"note", "delegate"' in source


def test_non_delegation_cannot_create_a_thread(tmp_path):
 m=load(); discord=Discord(); store=m.WorkStore(tmp_path/"work.db"); dispatcher=m.Dispatcher(store=store,discord=discord,parent_channel_id="parent",target_bot_id="bot")
 bad=record("c","ordinary follow-up"); bad["mode"]="note"
 with pytest.raises(m.DispatchError,match="explicit cross-agent delegation"): dispatcher.dispatch(bad)
 assert discord.created==[] and discord.posts==[]


def test_self_delegation_cannot_create_a_thread(tmp_path):
 m=load(); discord=Discord(); store=m.WorkStore(tmp_path/"work.db"); dispatcher=m.Dispatcher(store=store,discord=discord,parent_channel_id="parent",target_bot_id="bot")
 bad=record("d","my next plan step"); bad["target"]="mission"
 with pytest.raises(m.DispatchError,match="self-directed"): dispatcher.dispatch(bad)
 assert discord.created==[] and discord.posts==[]


def test_pair_reuses_one_thread_for_multiple_handoffs(tmp_path):
 m=load(); discord=Discord(); store=m.WorkStore(tmp_path/"work.db"); dispatcher=m.Dispatcher(store=store,discord=discord,parent_channel_id="parent",target_bot_id="bot")
 first=dispatcher.dispatch(record("a","first")); second=dispatcher.dispatch(record("b","second"))
 assert first["thread_id"]==second["thread_id"]=="thread-pair"
 assert len(discord.created)==1 and discord.reused==["thread-pair"]
 assert discord.members==[("thread-pair","bot"),("thread-pair","bot")]
 assert len(discord.posts)==2

def test_threads_auto_archive_after_one_hour():
 source=MODULE.read_text()
 assert '"auto_archive_duration": 60' in source
