from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


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


def test_disconnected_platform_recovery_uses_safe_reload_only(tmp_path):
    module = load()
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "gateway_state.json").write_text('{"gateway_state":"running"}')
    profile = module.ProfileBot("operator", home, ("discord",))
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="")

    assert module.attempt_recovery(
        profile,
        "discord disconnected",
        runner=runner,
        account_lookup=lambda _user: SimpleNamespace(pw_uid=1000, pw_dir="/home/operator"),
    )
    assert len(calls) == 1
    argv = calls[0][0]
    assert calls[0][1]["timeout"] == 360
    assert argv[0] == str(module.SAFE_RELOAD_SCRIPT)
    assert argv[1:] == [
        "--user", "operator",
        "--unit", "hermes-gateway.service",
        "--hermes-home", str(home),
        "--timeout", "60",
    ]
    assert "restart" not in argv and "stop" not in argv


def test_disconnected_recovery_timeout_fails_closed_without_raising(tmp_path):
    module = load()
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "gateway_state.json").write_text('{"gateway_state":"running"}')
    profile = module.ProfileBot("operator", home, ("discord",))

    def runner(*_args, **_kwargs):
        raise module.subprocess.TimeoutExpired("safe-reload", 360)

    assert not module.attempt_recovery(profile, "discord disconnected", runner=runner)


def test_disconnected_recovery_waits_then_uses_bounded_cooldown():
    module = load()
    reason = "discord disconnected"
    assert not module._should_attempt_recovery(None, reason, now=1_000)
    record = {"down_since": 1_000}
    assert not module._should_attempt_recovery(record, reason, now=1_119)
    assert module._should_attempt_recovery(record, reason, now=1_120)
    record["recovery_attempted_at"] = 1_120
    assert not module._should_attempt_recovery(record, reason, now=1_719)
    assert module._should_attempt_recovery(record, reason, now=1_720)


def test_watchdog_lock_prevents_overlapping_runs(tmp_path):
    module = load()
    state = tmp_path / "watchdog.json"
    first = module._acquire_watchdog_lock(state)
    assert first is not None
    try:
        assert module._acquire_watchdog_lock(state) is None
    finally:
        module._release_watchdog_lock(first)


def test_recovery_exception_records_cooldown_and_does_not_abort(tmp_path, monkeypatch):
    module = load()
    home = tmp_path / "operator" / ".hermes"
    profile = module.ProfileBot("operator", home, ("discord",))
    state_path = tmp_path / "watchdog.json"
    state_path.write_text(
        '{"%s":{"down_since":800,"reason":"discord disconnected","alerted":false}}' % home
    )
    monkeypatch.setattr(module, "discover_profile_bots", lambda _root: [profile])
    monkeypatch.setattr(module, "profile_health", lambda _profile: (False, "discord disconnected"))
    monkeypatch.setattr(module, "attempt_recovery", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    result = module.run_once(state_path=state_path, home_root=tmp_path, now=1_000)
    record = result[str(home)]
    assert record["recovery_attempted_at"] == 1_000
    assert record["recovery_succeeded"] is False


def test_successful_recovery_reprobes_before_alerting(tmp_path, monkeypatch):
    module = load()
    home = tmp_path / "operator" / ".hermes"
    profile = module.ProfileBot("operator", home, ("discord",))
    state_path = tmp_path / "watchdog.json"
    state_path.write_text(
        '{"%s":{"down_since":100,"reason":"discord disconnected","alerted":false}}' % home
    )
    health = iter([(False, "discord disconnected"), (True, "connected")])
    sent = []
    monkeypatch.setattr(module, "discover_profile_bots", lambda _root: [profile])
    monkeypatch.setattr(module, "profile_health", lambda _profile: next(health))
    monkeypatch.setattr(module, "attempt_recovery", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "notify_owner_dm", lambda *_args, **_kwargs: sent.append(True) or True)
    result = module.run_once(state_path=state_path, home_root=tmp_path, threshold=600, now=1_000)
    assert result == {}
    assert sent == []


def test_successful_recovery_suppresses_alert_while_reconnect_is_pending(tmp_path, monkeypatch):
    module = load()
    home = tmp_path / "operator" / ".hermes"
    profile = module.ProfileBot("operator", home, ("discord",))
    state_path = tmp_path / "watchdog.json"
    state_path.write_text(
        '{"%s":{"down_since":100,"reason":"discord disconnected","alerted":false}}' % home
    )
    health = iter([(False, "discord disconnected"), (False, "discord disconnected")])
    sent = []
    monkeypatch.setattr(module, "discover_profile_bots", lambda _root: [profile])
    monkeypatch.setattr(module, "profile_health", lambda _profile: next(health))
    monkeypatch.setattr(module, "attempt_recovery", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(module, "notify_owner_dm", lambda *_args, **_kwargs: sent.append(True) or True)
    result = module.run_once(state_path=state_path, home_root=tmp_path, threshold=600, now=1_000)
    assert str(home) in result
    assert result[str(home)]["alerted"] is False
    assert sent == []
