from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "overlay/hermes-core"
sys.path.insert(0, str(CORE))

from tools import clarify_gateway


def test_pending_clarify_count_tracks_unresolved_native_decisions():
    session = "reload-guard-test-session"
    clarify_gateway.clear_session(session)
    baseline = clarify_gateway.pending_count()

    clarify_gateway.register(
        "reload-guard-test",
        session,
        "Choose the deployment target",
        ["Stable", "Hold"],
    )
    try:
        assert clarify_gateway.pending_count() == baseline + 1
        assert clarify_gateway.has_pending(session)
        assert clarify_gateway.resolve_gateway_clarify("reload-guard-test", "Stable")
        assert clarify_gateway.pending_count() == baseline
    finally:
        clarify_gateway.clear_session(session)

    assert not clarify_gateway.has_pending(session)


def test_gateway_active_work_includes_pending_clarifications():
    source = (CORE / "gateway/run.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    methods = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_active_work_count", "_pending_clarify_count"}
    }
    namespace: dict[str, Any] = {}
    exec(
        compile(
            ast.Module(body=list(methods.values()), type_ignores=[]),
            "gateway-active-work-methods",
            "exec",
        ),
        namespace,
    )

    class Work:
        _running_agent_count = lambda self: 2
        _active_cron_job_count = lambda self: 3
        _active_api_run_count = lambda self: 5
        _pending_clarify_count = lambda self: 7

    assert namespace["_active_work_count"](Work()) == 17

    session = "reload-guard-method-test"
    clarify_gateway.clear_session(session)
    baseline = clarify_gateway.pending_count()
    clarify_gateway.register("reload-guard-method", session, "Choose", ["A", "B"])
    try:
        assert namespace["_pending_clarify_count"](object()) == baseline + 1
    finally:
        clarify_gateway.clear_session(session)
