import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, "/opt/agk-terminal/hermes-agent")

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "overlay/hermes/plugins/agentik_os/completion.py"
PLUGIN_INIT = MODULE.parent / "__init__.py"
TOOL_EXECUTOR = ROOT / "overlay/hermes-core/agent/tool_executor.py"


def load():
    spec = importlib.util.spec_from_file_location("station_plan_gate", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_operational_work_is_blocked_until_complete_plan_is_applied():
    module = load()
    identity = {"session_id": "session-1", "turn_id": "turn-1"}
    blocked = module.require_plan_before_work(tool_name="terminal", args={}, **identity)
    assert blocked["action"] == "block"
    plan = [{"id": "work", "content": "Do the work", "status": "in_progress"}]
    module.record_applied_plan(
        tool_name="todo",
        args={"todos": plan},
        result=json.dumps({"todos": plan}),
        status="ok",
        **identity,
    )
    assert module.require_plan_before_work(tool_name="terminal", args={}, **identity) is None


def test_invalid_or_read_only_todo_does_not_authorize_work():
    module = load()
    identity = {"session_id": "session-2", "turn_id": "turn-2"}
    module.record_applied_plan(
        tool_name="todo",
        args={},
        result='{"todos":[{"id":"old","content":"Old","status":"in_progress"}]}',
        status="ok",
        **identity,
    )
    assert module.require_plan_before_work(tool_name="write_file", args={}, **identity)["action"] == "block"


def test_plan_gate_hooks_are_registered():
    source = PLUGIN_INIT.read_text()
    assert 'ctx.register_hook("pre_tool_call", require_plan_before_work)' in source
    assert 'ctx.register_hook("post_tool_call", record_applied_plan)' in source


def test_sequential_executor_reports_terminal_success_status_to_post_tool_hooks():
    source = TOOL_EXECUTOR.read_text()
    marker = "if _executor_must_emit_post_hook:"
    block = source[source.index(marker):source.index("if not _execution_blocked:", source.index(marker))]
    assert 'status="error" if _is_error_result else "success"' in block


def test_prompt_requires_one_visible_discord_or_telegram_plan_before_work():
    prompt = load().completion_prompt()
    assert "before operational work" in prompt
    assert "Discord or Telegram" in prompt
    assert "update the same message" in prompt
    assert "without emitting per-tool notifications" in prompt
