from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "overlay/hermes/plugins/platforms/discord/interaction_surfaces.py"


def load():
    spec = importlib.util.spec_from_file_location("adaptive_decision_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def simple_request(m, **overrides):
    values = {
        "decision_id": "release-channel",
        "kind": m.SurfaceKind.SIMPLE,
        "title": "Choose release channel",
        "state": "The staged build is ready",
        "target": "Operator profile release pointer",
        "decision": "Which release channel should receive the build?",
        "choices": (
            m.DecisionChoice("stable", "Stable", "Move the pointer after verification", recommended=True, reason="Lowest operational risk"),
            m.DecisionChoice("hold", "Hold", "Leave the current release active"),
        ),
        "default_action": "Hold; the active release remains unchanged.",
        "source_session": "discord:operator:session-42",
        "expires_at": datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return m.DecisionRequest(**values)


def test_typed_request_binds_source_session_expiry_and_simple_visible_contract():
    m = load()
    request = simple_request(m)

    rendered = m.render_decision_surface(request)

    assert request.source_session == "discord:operator:session-42"
    assert request.expires_at == datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc)
    assert rendered.mode == "content"
    assert rendered.title == "Choose release channel"
    for text in (
        "The staged build is ready",
        "Operator profile release pointer",
        "Which release channel should receive the build?",
        "Hold; the active release remains unchanged.",
    ):
        assert text in rendered.body
    assert rendered.primary_label == "Confirm"
    assert rendered.detail_label == "Context"
    assert rendered.cancel_label == "Close"


def test_expiry_must_be_timezone_aware():
    m = load()
    with pytest.raises(ValueError, match="timezone-aware"):
        simple_request(m, expires_at=datetime(2026, 8, 31, 18, 0))


def test_complex_surface_shows_context_facts_recommendation_and_consequences():
    m = load()
    request = simple_request(
        m,
        kind=m.SurfaceKind.COMPLEX,
        context="Gareth requested a profile-safe canary release.",
        established=("Tests passed.", "The active pointer is unchanged."),
        recommendation="Choose Stable — it keeps rollback atomic.",
        context_detail="Artifact sha256: abc123",
    )

    rendered = m.render_decision_surface(request)

    assert rendered.mode == "embed"
    for text in (
        "Gareth requested a profile-safe canary release.",
        "Tests passed.",
        "The active pointer is unchanged.",
        "Stable — Move the pointer after verification",
        "Hold — Leave the current release active",
        "Choose Stable — it keeps rollback atomic.",
    ):
        assert text in rendered.body
    assert "Artifact sha256: abc123" not in rendered.body
    assert rendered.primary_label == "Continue"
    assert rendered.detail_label == "Technical context"
    assert rendered.cancel_label == "Close"


def test_risk_surface_shows_scope_rollback_and_exact_scope_confirmation():
    m = load()
    request = simple_request(
        m,
        kind=m.SurfaceKind.APPROVAL,
        context="A gateway update is staged.",
        established=("The staged artifact passed checks.",),
        risk="A restart briefly interrupts Operator replies.",
        includes=("Operator profile gateway", "One staged release pointer"),
        excludes=("Mission, Private, and client profiles", "Credentials and session state"),
        rollback="Restore the previous release pointer and restart Operator.",
    )

    rendered = m.render_decision_surface(request)
    confirmation = m.render_exact_scope_confirmation(request)

    assert rendered.semantic_color == "warning"
    for text in (
        "CHANGE\nWhich release channel should receive the build?",
        "RISK\nA restart briefly interrupts Operator replies.",
        "INCLUDES\n- Operator profile gateway\n- One staged release pointer",
        "EXCLUDES\n- Mission, Private, and client profiles\n- Credentials and session state",
        "ROLLBACK\nRestore the previous release pointer and restart Operator.",
    ):
        assert text in rendered.body
    assert rendered.primary_label == "Review & approve"
    assert rendered.detail_label == "Evidence"
    assert rendered.cancel_label == "Cancel"
    assert confirmation.ephemeral is True
    assert "Operator profile gateway" in confirmation.body
    assert "Mission, Private, and client profiles" in confirmation.body
    assert confirmation.confirm_custom_id == "decision:release-channel:approve"
    assert confirmation.cancel_custom_id == "decision:release-channel:cancel"


def test_open_text_uses_focused_modal_without_repeating_question():
    m = load()
    request = simple_request(
        m,
        kind=m.SurfaceKind.OPEN_TEXT,
        decision="What constraint should the rollout preserve?",
        choices=(),
    )

    rendered = m.render_decision_surface(request)
    modal = m.render_open_text_modal(request)

    assert rendered.mode == "content"
    assert rendered.primary_label == "Write response"
    assert rendered.detail_label == "Context"
    assert rendered.body.count(request.decision) == 1
    assert modal.title == "Write response"
    assert modal.input_label == "Response"
    assert modal.placeholder == "Type the information needed to continue"
    assert request.decision not in modal.title
    assert request.decision not in modal.input_label
    assert request.decision not in modal.placeholder
    assert modal.custom_id == "decision:release-channel:text"


def test_batch_renders_two_to_five_independent_questions_once():
    m = load()
    first = simple_request(m, decision_id="channel")
    second = simple_request(
        m,
        decision_id="window",
        target="Operator restart window",
        decision="When should the verified restart run?",
    )
    batch = simple_request(
        m,
        decision_id="release-batch",
        kind=m.SurfaceKind.BATCH,
        context="Two independent rollout choices are ready.",
        established=("Neither answer changes the other question.",),
        batch_items=(first, second),
    )

    rendered = m.render_decision_surface(batch)

    assert rendered.mode == "embed"
    assert rendered.body.count(first.decision) == 1
    assert rendered.body.count(second.decision) == 1
    assert "QUESTION 1" in rendered.body
    assert "QUESTION 2" in rendered.body

    with pytest.raises(ValueError, match="two to five"):
        simple_request(
            m,
            decision_id="invalid-batch",
            kind=m.SurfaceKind.BATCH,
            context="Only one question.",
            established=("It is independent.",),
            batch_items=(first,),
        )


def test_component_blueprint_uses_selects_stable_ids_and_required_controls():
    m = load()
    request = simple_request(m)

    controls = m.build_component_blueprint(request)

    assert [control.kind for control in controls] == ["select", "button", "button", "button"]
    select, confirm, context, close = controls
    assert select.custom_id == "decision:release-channel:select"
    assert [(option.value, option.label, option.description) for option in select.options] == [
        ("stable", "Stable", "Move the pointer after verification"),
        ("hold", "Hold", "Leave the current release active"),
    ]
    assert (confirm.label, confirm.custom_id) == ("Confirm", "decision:release-channel:confirm")
    assert (context.label, context.custom_id) == ("Context", "decision:release-channel:context")
    assert (close.label, close.custom_id) == ("Close", "decision:release-channel:close")

    complex_controls = m.build_component_blueprint(
        simple_request(
            m,
            kind=m.SurfaceKind.COMPLEX,
            context="Canary context.",
            established=("Checks passed.",),
        )
    )
    assert [control.label for control in complex_controls[1:]] == [
        "Continue", "Technical context", "Close"
    ]


def test_lifecycle_rechecks_full_authorization_and_resolves_atomically():
    m = load()
    request = simple_request(m, expires_at=datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc))
    binding = m.AuthorizationBinding(
        user_ids=frozenset({"42"}),
        role_ids=frozenset({"7"}),
        guild_id="guild-1",
        channel_id="channel-1",
        profile_id="operator",
        target=request.target,
    )
    authorized = m.CallbackContext(
        user_id="42",
        role_ids=frozenset(),
        guild_id="guild-1",
        channel_id="channel-1",
        profile_id="operator",
        target=request.target,
    )
    lifecycle = m.DecisionLifecycle(request, binding)

    unauthorized = m.CallbackContext(
        user_id="99",
        role_ids=frozenset(),
        guild_id="guild-1",
        channel_id="channel-1",
        profile_id="operator",
        target=request.target,
    )
    assert lifecycle.resolve(unauthorized, "stable", now=datetime(2026, 8, 31, 17, 0, tzinfo=timezone.utc)).status == "unauthorized"

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: lifecycle.resolve(
                    authorized,
                    "stable",
                    now=datetime(2026, 8, 31, 17, 0, tzinfo=timezone.utc),
                ),
                range(8),
            )
        )
    assert [result.status for result in results].count("accepted") == 1
    assert [result.status for result in results].count("already_resolved") == 7
    assert lifecycle.snapshot().controls_disabled is True
    assert lifecycle.snapshot().selected_value == "stable"
    assert lifecycle.snapshot().source_session == "discord:operator:session-42"


def test_expired_lifecycle_is_inactive_and_preserves_safe_default():
    m = load()
    request = simple_request(m, expires_at=datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc))
    binding = m.AuthorizationBinding(
        user_ids=frozenset({"42"}),
        role_ids=frozenset(),
        guild_id="guild-1",
        channel_id="channel-1",
        profile_id="operator",
        target=request.target,
    )
    context = m.CallbackContext(
        user_id="42",
        role_ids=frozenset(),
        guild_id="guild-1",
        channel_id="channel-1",
        profile_id="operator",
        target=request.target,
    )
    lifecycle = m.DecisionLifecycle(request, binding)

    result = lifecycle.resolve(
        context,
        "stable",
        now=datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc),
    )

    assert result.status == "expired"
    assert lifecycle.snapshot().controls_disabled is True
    assert lifecycle.snapshot().default_action == request.default_action


def test_utf16_truncation_preserves_actionable_blocks_before_long_context():
    m = load()
    request = simple_request(
        m,
        kind=m.SurfaceKind.COMPLEX,
        title="🚀" * 400,
        state="Waiting " + "🧪" * 400,
        context="Background " + "📚" * 1000,
        established=("Verification complete.",),
        context_detail="Evidence " + "🔎" * 1000,
    )

    rendered = m.render_decision_content(request, limit=700)

    assert m.utf16_len(rendered) <= 700
    for actionable in (
        request.target,
        request.decision,
        "Stable — Move the pointer after verification",
        "Hold — Leave the current release active",
        request.default_action,
    ):
        assert actionable in rendered


def test_legacy_clarify_safely_forms_approved_simple_or_open_text_request():
    m = load()
    request = m.decision_request_from_clarify(
        question="Choose the Operator release channel",
        choices=("Stable (Recommended)", "Hold"),
        clarify_id="clarify-7",
        source_session="discord:operator:session-7",
        surface=None,
    )

    assert request.kind is m.SurfaceKind.SIMPLE
    assert request.title == "Choose the Operator release channel"
    assert request.target == "Current request in this session"
    assert request.decision == "Choose the Operator release channel"
    assert request.source_session == "discord:operator:session-7"
    assert request.choices[0].id == "choice-1"
    assert request.choices[0].label == "Stable"
    assert request.choices[0].recommended is True
    assert request.default_action == "No action; work remains paused until answered."
    assert "Hermes needs your input" not in m.render_decision_surface(request).body

    open_request = m.decision_request_from_clarify(
        question="Name the rollout constraint",
        choices=(),
        clarify_id="clarify-8",
        source_session="discord:operator:session-8",
        surface=None,
    )
    assert open_request.kind is m.SurfaceKind.OPEN_TEXT


@pytest.mark.parametrize(
    "surface",
    (
        {"title": "Release choice"},
        {"context": "The release is staged but not active."},
        {"recommendation": "Prefer Stable."},
    ),
)
def test_partial_structured_clarify_remains_a_valid_safe_simple_surface(surface):
    m = load()
    request = m.decision_request_from_clarify(
        question="Choose the Operator release channel",
        choices=("Stable (Recommended)", "Hold"),
        clarify_id="clarify-partial",
        source_session="discord:operator:session-partial",
        surface=surface,
    )

    assert request.kind is m.SurfaceKind.SIMPLE
    assert request.state == "Work is paused for this answer."
    assert request.target == "Current request in this session"
    assert request.default_action == "No action; work remains paused until answered."
    assert all(choice.consequence for choice in request.choices)


def test_open_text_surface_omits_empty_context_sections():
    m = load()
    request = m.decision_request_from_clarify(
        question="Describe the intended result",
        choices=(),
        clarify_id="clarify-open",
        source_session="discord:operator:open",
        surface=None,
    )

    rendered = m.render_decision_surface(request).body
    assert "CONTEXT\n" not in rendered
    assert "ESTABLISHED\n" not in rendered


def test_invalid_optional_kind_and_expiry_degrade_safely():
    m = load()
    request = m.decision_request_from_clarify(
        question="Choose release",
        choices=("Stable", "Hold"),
        clarify_id="clarify-invalid-optional",
        source_session="discord:operator:invalid",
        surface={"kind": "not-a-kind", "expires_at": "not-a-date"},
    )

    assert request.kind is m.SurfaceKind.SIMPLE
    assert request.expires_at is None


def test_detail_truncation_never_exceeds_utf16_limit():
    m = load()
    request = m.DecisionRequest(
        decision_id="detail-budget",
        kind=m.SurfaceKind.COMPLEX,
        title="Release",
        state="Staged",
        context="Context",
        established=("Tests pass",),
        target="Operator",
        decision="Choose release",
        choices=(m.DecisionChoice("stable", "Stable", "Activate stable"),),
        default_action="Hold",
        context_detail="😀" * 500,
        source_session="discord:operator:detail",
    )

    rendered = m.render_decision_content(request, limit=300)
    assert m.utf16_len(rendered) <= 300
