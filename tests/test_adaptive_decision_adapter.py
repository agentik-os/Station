from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "overlay/hermes/plugins/platforms/discord/adapter.py"


def method(source: str, name: str, next_name: str) -> str:
    start = source.index(f"    async def {name}(")
    end = source.index(f"    async def {next_name}(", start)
    return source[start:end]


def test_send_clarify_delegates_to_typed_adaptive_surface_without_legacy_copy():
    source = ADAPTER.read_text(encoding="utf-8")
    clarify = method(source, "send_clarify", "send_update_prompt")

    assert "decision_request_from_clarify(" in clarify
    assert "AdaptiveDecisionView(" in clarify
    assert "return await self.send_decision(" in clarify
    assert "ClarifyChoiceView(" not in clarify
    assert "Hermes needs your input" not in clarify
    assert "render_compact_clarify_content(" not in clarify
    assert "SurfaceKind.COMPLEX" not in clarify


def test_adaptive_view_uses_selects_modal_auth_second_stage_and_same_message_edits():
    source = ADAPTER.read_text(encoding="utf-8")
    start = source.index("    class AdaptiveDecisionView(")
    end = source.index("    class ClarifyChoiceView(", start)
    view = source[start:end]

    assert "discord.ui.Select(" in view
    assert "discord.ui.Modal" in view
    assert "self._check_auth(interaction)" in view
    assert "render_exact_scope_confirmation" in view
    assert "ephemeral=True" in view
    assert "resolve_gateway_clarify" in view
    assert "child.disabled = True" in view
    assert "interaction.response.edit_message" in view
    assert "Prompt expired" in view
    assert "mark_awaiting_text" not in view


def test_send_decision_edits_existing_canonical_message_on_retry():
    source = ADAPTER.read_text(encoding="utf-8")
    decision = method(source, "send_decision", "send_exec_approval")

    assert 'metadata.get("decision_message_id")' in decision
    assert "self._decision_message_ids.get(decision_key)" in decision
    assert "self._decision_message_ids[decision_key] = str(message.id)" in decision
    assert "fetch_message(" in decision
    assert "message.edit(**kwargs)" in decision
    assert "channel.send(**kwargs)" in decision


def test_open_text_plain_modal_and_cancel_paths_do_not_race_or_block():
    source = ADAPTER.read_text(encoding="utf-8")
    decision = method(source, "send_decision", "send_exec_approval")
    start = source.index("    class AdaptiveDecisionView(")
    end = source.index("    class ClarifyChoiceView(", start)
    view = source[start:end]

    assert 'rendered_surface.mode == "content"' in decision
    assert "mark_awaiting_text" not in view
    assert "class DecisionTextModal(" in view
    assert "custom_id=modal_spec.custom_id" in view
    assert 'cancel_value = f"Cancelled — {self.request.default_action}"' in view
    assert "resolve_gateway_clarify(self.clarify_id, resolved_value)" in view
    assert "await parent._cancel(submitted, from_private=True)" in view
