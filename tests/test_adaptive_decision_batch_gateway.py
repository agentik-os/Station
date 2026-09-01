from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "overlay/hermes-core/gateway/run.py"
ADAPTER = ROOT / "overlay/hermes/plugins/platforms/discord/adapter.py"


def test_gateway_dispatches_one_native_batch_and_preserves_other_platform_fallback():
    source = RUN.read_text(encoding="utf-8")
    start = source.index("        def _clarify_callback_sync(")
    end = source.index("        agent.clarify_callback = _clarify_callback_sync", start)
    block = source[start:end]

    assert "questions=None" in block
    assert "send_clarify_batch" in block
    assert '"source_session"' in block
    assert '"answers"' in block
    assert "_clarify_callback_sync(" in block  # sequential compatibility fallback


def test_discord_batch_is_one_message_with_navigation_review_and_atomic_json_resolution():
    source = ADAPTER.read_text(encoding="utf-8")

    assert "async def send_clarify_batch(" in source
    assert "class AdaptiveBatchDecisionView(discord.ui.View):" in source
    batch_start = source.index("    class AdaptiveBatchDecisionView(discord.ui.View):")
    batch_end = source.index("    class ClarifyChoiceView(discord.ui.View):", batch_start)
    block = source[batch_start:batch_end]

    for required in (
        "Previous",
        "Next",
        "Review answers",
        "Close",
        "Write response",
        "discord.ui.Select",
        "discord.ui.Modal",
        "resolve_gateway_clarify",
        'json.dumps({"answers": self.answers',
        "self._resolution_lock",
        "ephemeral=True",
    ):
        assert required in block

    # Batch is still delivered through the canonical one-message decision sender.
    method_start = source.index("    async def send_clarify_batch(")
    method_end = source.index("    async def send_update_prompt(", method_start)
    method = source[method_start:method_end]
    assert "AdaptiveBatchDecisionView(" in method
    assert "return await self.send_decision(" in method
