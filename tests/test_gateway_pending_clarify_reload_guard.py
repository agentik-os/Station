from __future__ import annotations

import sys
from pathlib import Path

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
    start = source.index("    def _active_work_count(self) -> int:")
    end = source.index("    def _active_cron_job_count(self) -> int:", start)
    active_work = source[start:end]

    assert "+ self._pending_clarify_count()" in active_work
    assert "def _pending_clarify_count(self) -> int:" in source
    assert "clarify_gateway.pending_count()" in source
