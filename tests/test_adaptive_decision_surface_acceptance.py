from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SURFACES = ROOT / "overlay/hermes/plugins/platforms/discord/interaction_surfaces.py"
ADAPTER = ROOT / "overlay/hermes/plugins/platforms/discord/adapter.py"


def load_surfaces():
    spec = importlib.util.spec_from_file_location("adaptive_surface_acceptance", SURFACES)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def choice(m, ident: str, label: str, consequence: str, recommended: bool = False):
    return m.DecisionChoice(ident, label, consequence, recommended=recommended)


def request(m, kind, **overrides):
    values = dict(
        decision_id="decision-123",
        title="Choose the rollout strategy",
        state="The release candidate passed isolated tests. Production is unchanged.",
        target="Operator → Agentik → Mission → Private → Collective",
        decision="Select how broadly the release should be activated.",
        choices=(
            choice(m, "canary", "Canary, then staged rollout", "Lowest risk; stop on failed readback.", True),
            choice(m, "none", "Do not activate", "Keep every active gateway unchanged."),
        ),
        default_action="No response leaves production unchanged.",
        kind=kind,
        context="The duplicate-question fix is ready in a staged release.",
        established=("Tests pass.", "Rollback artifacts exist."),
        recommendation="Canary Operator first, then continue only while health remains green.",
        risk="The active Discord interaction path changes across Station gateways.",
        includes=("Adapter", "Structured decision schema", "Fleet readback"),
        excludes=("Secrets", "Sessions", "Memories", "Client content"),
        rollback="Restore the previous release pointer on any failed readback.",
        context_detail="Technical evidence remains available on demand.",
        source_session="session-123",
    )
    values.update(overrides)
    return m.DecisionRequest(**values)


def test_simple_surface_matches_approved_information_order():
    m = load_surfaces()
    req = request(
        m,
        m.SurfaceKind.SIMPLE,
        context="",
        established=(),
        recommendation="",
        risk="",
        includes=(),
        excludes=(),
        rollback="",
    )
    text = m.render_decision_content(req)
    assert text.index("Choose the rollout strategy") < text.index("TARGET") < text.index("DECISION")
    assert "Operator → Agentik" in text
    assert "No response leaves production unchanged." in text
    assert "Hermes needs your input" not in text
    assert "CONTEXT" not in text


def test_complex_surface_keeps_required_context_and_choice_consequences():
    m = load_surfaces()
    req = request(m, m.SurfaceKind.COMPLEX, risk="", includes=(), excludes=(), rollback="")
    text = m.render_decision_content(req)
    for required in (
        "CONTEXT",
        "The duplicate-question fix is ready",
        "ESTABLISHED",
        "Tests pass.",
        "TARGET",
        "DECISION",
        "RECOMMENDATION",
        "Canary Operator first",
        "CHOICES",
        "Lowest risk; stop on failed readback.",
        "DEFAULT",
    ):
        assert required in text


def test_risk_surface_keeps_exact_scope_and_rollback():
    m = load_surfaces()
    text = m.render_decision_content(request(m, m.SurfaceKind.RISK))
    for required in ("RISK", "INCLUDES", "EXCLUDES", "ROLLBACK", "Secrets", "previous release pointer"):
        assert required in text


def test_utf16_limit_preserves_decision_target_and_safe_default_before_context():
    m = load_surfaces()
    req = request(
        m,
        m.SurfaceKind.COMPLEX,
        context="Older background. " * 600,
        context_detail="Evidence. " * 600,
    )
    text = m.render_decision_content(req, limit=2000)
    assert m.utf16_len(text) <= 2000
    assert req.target in text
    assert req.decision in text
    assert req.default_action in text
    assert "Context shortened" in text


def test_contract_requires_source_session_and_risk_scope():
    m = load_surfaces()
    assert "source_session" in m.DecisionRequest.__dataclass_fields__
    with pytest.raises(ValueError):
        request(m, m.SurfaceKind.RISK, includes=())


def test_adapter_uses_adaptive_controls_and_never_forces_complex_or_generic_title():
    source = ADAPTER.read_text()
    clarify = source[source.index("    async def send_clarify("):source.index("    async def send_update_prompt(")]
    assert "kind=SurfaceKind.COMPLEX" not in clarify
    assert "Hermes needs your input" not in clarify
    assert "decision_request_from_clarify(" in clarify
    surface_source = SURFACES.read_text()
    for field in ("context", "established", "target", "recommendation", "risk", "includes", "excludes", "rollback", "source_session"):
        assert f'surface.get("{field}")' in surface_source
    for control in ("Confirm", "Context", "Close", "Continue", "Technical context", "Review & approve", "Evidence", "Cancel"):
        assert control in surface_source
    assert "discord.ui.Select" in source
    assert "ephemeral=True" in source
    assert "resolve_gateway_clarify" in source


def test_open_text_uses_modal_without_repeating_full_question():
    source = ADAPTER.read_text()
    assert "Write response" in source
    assert "discord.ui.Modal" in source
    assert "TextInput" in source
    assert "full_question" not in source


def test_clarify_tool_carries_adaptive_kind_and_exact_approval_scope():
    tool = (ROOT / "overlay/hermes-core/tools/clarify_tool.py").read_text()
    for argument in ("kind", "includes", "excludes", "rollback", "context_detail"):
        assert f'"{argument}"' in tool
    for forwarded in (
        "kind=kind",
        "includes=includes",
        "excludes=excludes",
        "rollback=rollback",
        "context_detail=context_detail",
    ):
        assert forwarded in tool


def test_installation_uses_one_canonical_adaptive_clarify_schema():
    install = (ROOT / "overlay/install.sh").read_text()
    shared = (ROOT / "overlay/scripts/install-shared-hermes.sh").read_text()
    assert '"$repo_root/hermes-core/tools/clarify_tool.py"' in install
    assert '"$repo_root/hermes/tools/clarify_tool.py"' not in install
    assert '"$install_root/hermes-core/tools/clarify_tool.py"' in shared
    assert '"$install_root/hermes/tools/clarify_tool.py"' not in shared


def test_gateway_binds_decision_surface_to_the_exact_source_session():
    gateway = (ROOT / "overlay/hermes-core/gateway/run.py").read_text()
    callback = gateway[
        gateway.index("        def _clarify_callback_sync("):
        gateway.index("        agent.clarify_callback = _clarify_callback_sync")
    ]
    assert "surface_payload = dict(surface or {})" in callback
    assert 'surface_payload["source_session"] = ctx.session_key or ""' in callback
    assert '"decision_user_id": str(ctx.source.user_id)' in callback
    assert "if not ctx.source.user_id:" in callback
    assert "[clarify source user unavailable]" in callback
    assert "surface=surface_payload or None" in callback
    assert '{"decision_surface": surface_payload}' in callback
