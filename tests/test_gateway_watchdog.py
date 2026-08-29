from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "overlay/scripts/gateway_watchdog.py"


def load():
    spec = importlib.util.spec_from_file_location("gateway_watchdog_under_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_outage_notification_targets_authorized_owner_dm_only(tmp_path, monkeypatch):
    module = load()
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / ".env").write_text("DISCORD_BOT_TOKEN=validated-secret\n", encoding="utf-8")
    calls = []

    def discord_json(token, method, path, payload=None):
        calls.append((token, method, path, payload))
        if method == "POST" and path == "/users/@me/channels":
            return {"id": "1999999999999999999"}
        if method == "POST" and path == "/channels/1999999999999999999/messages":
            return {"id": "1888888888888888888"}
        raise AssertionError(f"unexpected Discord request: {method} {path}")

    monkeypatch.setattr(module, "_discord_json", discord_json)

    assert module.notify_owner_dm(
        home,
        "1441423462492016821",
        "Nutrition OS is offline.",
    )
    assert calls == [
        (
            "validated-secret",
            "POST",
            "/users/@me/channels",
            {"recipient_id": "1441423462492016821"},
        ),
        (
            "validated-secret",
            "POST",
            "/channels/1999999999999999999/messages",
            {"content": "Nutrition OS is offline.", "allowed_mentions": {"parse": []}},
        ),
    ]


def test_watchdog_source_has_no_general_channel_alert_path():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "notify_general" not in source
    assert "_discord_general_target" not in source
