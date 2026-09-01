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
    assert "Expired — no response received; no action was taken." in view
    assert "mark_awaiting_text" not in view


def test_send_decision_edits_existing_canonical_message_on_retry():
    source = ADAPTER.read_text(encoding="utf-8")
    decision = method(source, "send_decision", "send_exec_approval")

    assert 'metadata.get("decision_message_id")' in decision
    assert "request.source_session" in decision
    assert "self._decision_message_ids.get(decision_key)" in decision
    assert "self._decision_message_ids[decision_key] = str(message.id)" in decision
    assert "fetch_message(" in decision
    assert "message.edit(**kwargs)" in decision
    assert "channel.send(**kwargs)" in decision
    assert "except discord.NotFound:" in decision
    assert "self._decision_send_locks" in decision
    assert "async with decision_lock:" in decision
    assert decision.index("self._decision_message_ids.get(decision_key)") < decision.index(
        'metadata.get("decision_message_id")'
    )


def test_single_clarify_forwards_multiselect_and_supports_all_options():
    source = ADAPTER.read_text(encoding="utf-8")
    clarify = method(source, "send_clarify", "send_update_prompt")
    view = source[
        source.index("    class AdaptiveDecisionView(discord.ui.View):"):
        source.index("    class AdaptiveBatchDecisionView(discord.ui.View):")
    ]

    assert 'multi_select=bool((metadata or {}).get("multi_select"))' in clarify
    assert 'ALL_OPTIONS_VALUE = "__all__"' in view
    assert 'OTHER_VALUE = "__other__"' in view
    assert "max_values=len(options)" in view
    assert '"Choose one or more…"' in view
    assert "self.selected_values" in view
    assert "json.dumps(resolved_values" in view
    assert "self.OTHER_VALUE in self.selected_values" in view


def test_resolved_and_expired_decisions_remove_stale_controls_and_avoid_default_duplication():
    source = ADAPTER.read_text(encoding="utf-8")
    view = source[
        source.index("    class AdaptiveDecisionView(discord.ui.View):"):
        source.index("    class AdaptiveBatchDecisionView(discord.ui.View):")
    ]

    assert "content=content, embed=embed, view=None" in view
    assert '"No response received; no action was taken."' in view
    assert "await message.edit(content=content, embed=embed, view=None)" in view


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
    assert "gateway_resolved = resolve_gateway_clarify(" in view
    assert "await parent._cancel(submitted, from_private=True)" in view


def test_adaptive_view_reports_success_only_after_gateway_acceptance():
    source = ADAPTER.read_text()
    view = source[
        source.index("    class AdaptiveDecisionView(discord.ui.View):"):
        source.index("    class AdaptiveBatchDecisionView(discord.ui.View):")
    ]

    assert "gateway_resolved = resolve_gateway_clarify(" in view
    assert "if gateway_resolved" in view
    assert "Response was not accepted" in view


def test_adaptive_view_snapshots_confirmation_and_bounds_every_status_path():
    source = ADAPTER.read_text()
    view = source[
        source.index("    class AdaptiveDecisionView(discord.ui.View):"):
        source.index("    class AdaptiveBatchDecisionView(discord.ui.View):")
    ]

    assert "reviewed_value = self.selected_value" in view
    assert "selected_value=reviewed_value" in view
    assert "parent.selected_value != scope_self.reviewed_value" in view
    assert "Selection changed after this confirmation opened" in view
    assert "scope_self.reviewed_value" in view
    assert "detail[:1900]" not in view
    assert "truncate_station_text(detail, 1900)" in view
    assert "append_station_status(" in view
    assert "Cancelled. No action was applied by this confirmation." in view
    assert "safe default was applied" not in view


def test_conversational_and_approval_paths_use_utf16_budgets():
    source = ADAPTER.read_text()

    assert "if utf16_len(formatted) > self.MAX_MESSAGE_LENGTH:" in source
    assert source.count("len_fn=utf16_len") >= 6
    approval = method(source, "send_exec_approval", "send_slash_confirm")
    assert "truncate_station_text(" in approval
    assert "sanitize_visible_text(command)" in approval


def test_adaptive_view_timeout_unblocks_the_gateway_with_the_safe_default():
    source = ADAPTER.read_text()
    view = source[
        source.index("    class AdaptiveDecisionView(discord.ui.View):"):
        source.index("    class AdaptiveBatchDecisionView(discord.ui.View):")
    ]

    assert "from tools.clarify_tool import TIMEOUT_RESPONSE" in view
    assert "resolve_gateway_clarify(self.clarify_id, TIMEOUT_RESPONSE)" in view


def test_adaptive_views_bind_callbacks_to_the_initiating_user():
    source = ADAPTER.read_text()
    single = source[
        source.index("    class AdaptiveDecisionView(discord.ui.View):"):
        source.index("    class AdaptiveBatchDecisionView(discord.ui.View):")
    ]
    batch = source[
        source.index("    class AdaptiveBatchDecisionView(discord.ui.View):"):
        source.index("    class ClarifyChoiceView(discord.ui.View):")
    ]

    for view in (single, batch):
        assert "responder_user_id" in view
        assert "interaction_user_id == self.responder_user_id" in view


def test_batch_view_is_registered_as_a_module_global():
    source = ADAPTER.read_text(encoding="utf-8")
    global_line = next(
        line for line in source.splitlines()
        if line.strip().startswith("global ExecApprovalView")
    )

    assert "AdaptiveBatchDecisionView" in global_line
