from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "overlay/hermes/plugins/platforms/discord/interaction_surfaces.py"


def load():
    spec = importlib.util.spec_from_file_location("complete_clarify_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_oauth_complex_surface_preserves_decision_context_target_and_consequences():
    m = load()
    url = "https://connect.example/link/abc"
    request = m.DecisionRequest(
        decision_id="granola-oauth",
        title="Connect Granola",
        state="Waiting for OAuth consent",
        target="Agentik Composio account",
        decision=f"Complete authorization at {url}, then choose the result.",
        choices=(
            m.DecisionChoice("done", "Authorization done", "Verify and continue", recommended=True),
            m.DecisionChoice("failed", "Link failed", "Diagnose without activation"),
        ),
        default_action="Leave the connection pending.",
        kind=m.SurfaceKind.COMPLEX,
        context="The owner approved a secure OAuth connection.",
        established=("The connection exists but is not authorized.",),
        recommendation="Choose Authorization done only after consent succeeds.",
        risk="Grants access to meeting metadata and transcripts.",
        source_session="discord:agentik:oauth",
    )

    rendered = m.render_decision_surface(request)

    assert rendered.mode == "embed"
    assert rendered.body.count(url) == 1
    for text in (
        request.context,
        request.established[0],
        request.target,
        request.decision,
        "Authorization done — Verify and continue",
        "Link failed — Diagnose without activation",
        request.recommendation,
        request.default_action,
    ):
        assert text in rendered.body


def test_adapter_routes_clarify_through_typed_adaptive_decision():
    source = (ROOT / "overlay/hermes/plugins/platforms/discord/adapter.py").read_text()
    clarify = source[source.index("    async def send_clarify("):source.index("    async def send_update_prompt(")]
    assert "decision_request_from_clarify(" in clarify
    assert "AdaptiveDecisionView(" in clarify
    assert "return await self.send_decision(" in clarify
    assert "Hermes needs your input" not in clarify
    assert "render_compact_clarify_content(" not in clarify
