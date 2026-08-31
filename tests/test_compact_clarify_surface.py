from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "overlay/hermes/plugins/platforms/discord/interaction_surfaces.py"


def load():
    spec = importlib.util.spec_from_file_location("compact_clarify_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_oauth_clarify_is_one_compact_surface_without_repeated_context():
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
        context=f"Open the secure authorization page: {url}",
        established=("The owner approved creating the connection.",),
        recommendation="Authorization done after consent succeeds.",
        risk="Grants access to meeting metadata and transcripts.",
    )

    content = m.render_compact_clarify_content(request)

    assert content.count(url) == 1
    assert content.count(request.decision) == 1
    assert "Connect Granola" in content
    assert "Waiting for OAuth consent" in content
    assert "RISK\nGrants access to meeting metadata and transcripts." in content
    assert "DEFAULT\nLeave the connection pending." in content
    for required_heading in ("TARGET", "CHOICES", "RECOMMENDATION", "ESTABLISHED", "CONTEXT"):
        assert required_heading in content
    assert "Verify and continue" in content


def test_adapter_routes_decision_backed_clarify_to_compact_plain_content():
    source = (ROOT / "overlay/hermes/plugins/platforms/discord/adapter.py").read_text()
    clarify = source[source.index("    async def send_clarify("):source.index("    async def send_update_prompt(")]
    assert "render_compact_clarify_content(" in clarify
    assert "channel.send(content=content, view=view)" in clarify
    assert "return await self.send_decision(" not in clarify
