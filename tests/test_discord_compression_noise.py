import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "overlay/hermes-core/gateway/station_noise_policy.py"


def load():
    spec = importlib.util.spec_from_file_location("station_noise_policy_test", MODULE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_discord_suppresses_automatic_compression_timeout_and_blocked_noise():
    module = load()
    messages = (
        "⚠ Context compression timed out after 120.0s with no output from the summary model. No messages were dropped — continuing without compression.",
        "⚠ Context compression is temporarily unavailable for this session (no summary output after 120.0s). No messages were dropped.",
        "⚠ Context is over the compression threshold (~620,995 tokens >= 231,200) but compression is currently blocked (cooldown:60). The model may stop responding.",
    )
    for message in messages:
        assert module.suppress_automatic_compression_notice("discord", message) is True
        assert module.suppress_automatic_compression_notice("local", message) is False


def test_discord_keeps_manual_compression_and_provider_failures_visible():
    module = load()
    for message in (
        "Compressed: 30 → 12 messages",
        "Compression aborted: 30 messages preserved",
        "Provider authentication failed. Reconnect the account.",
    ):
        assert module.suppress_automatic_compression_notice("discord", message) is False
