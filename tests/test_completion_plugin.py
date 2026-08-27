import importlib.util
import os
from pathlib import Path

MODULE=Path(__file__).resolve().parents[1]/"overlay/hermes/plugins/agentik_os/completion.py"


def load():
 spec=importlib.util.spec_from_file_location("completion_plugin_test",MODULE); assert spec and spec.loader
 module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module


def test_pre_llm_hook_archives_original_user_prompt_once(tmp_path,monkeypatch):
 module=load(); monkeypatch.setenv("HERMES_HOME",str(tmp_path/".hermes")); monkeypatch.setenv("AGK_TERMINAL_ROOT",str(Path(__file__).resolve().parents[1]/"overlay"))
 module.archive_before_execution(session_id="s1",platform="discord",turn_id="t1",user_message="Do A, B and C")
 module.archive_before_execution(session_id="s1",platform="discord",turn_id="t1",user_message="Do A, B and C")
 db=module.open_store().db
 assert db.execute("SELECT COUNT(*) FROM prompts").fetchone()[0]==1
 assert Path(db.execute("SELECT content_path FROM prompts").fetchone()[0]).read_text()=="Do A, B and C"
