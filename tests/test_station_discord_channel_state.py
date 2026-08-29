from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).parents[1] / "overlay" / "scripts" / "station_discord_channel_state.py"
SPEC = importlib.util.spec_from_file_location("station_discord_channel_state", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

INSTALLER_PATH = Path(__file__).parents[1] / "overlay" / "scripts" / "install-station-discord-channel-state.py"
INSTALLER_SPEC = importlib.util.spec_from_file_location("install_station_discord_channel_state", INSTALLER_PATH)
assert INSTALLER_SPEC and INSTALLER_SPEC.loader
INSTALLER = importlib.util.module_from_spec(INSTALLER_SPEC)
INSTALLER_SPEC.loader.exec_module(INSTALLER)


def test_desired_name_keeps_identity_and_projects_state() -> None:
    assert MODULE.desired_name("operator", "working") == "operator・working"
    assert MODULE.desired_name("operator", "idle") == "operator・idle"


@pytest.mark.parametrize("state", ["unknown", "done", "", "WORKING"])
def test_desired_name_rejects_unknown_state(state: str) -> None:
    with pytest.raises(ValueError, match="unsupported channel state"):
        MODULE.desired_name("operator", state)


def test_runtime_state_uses_active_count_and_typed_expiring_override() -> None:
    assert MODULE.resolve_state(active_agents=0, override=None, now=100) == "idle"
    assert MODULE.resolve_state(active_agents=2, override=None, now=100) == "working"
    assert MODULE.resolve_state(
        active_agents=2,
        override={"state": "blocked", "expires_at": 101},
        now=100,
    ) == "blocked"
    assert MODULE.resolve_state(
        active_agents=2,
        override={"state": "approval", "expires_at": 99},
        now=100,
    ) == "working"
    assert MODULE.resolve_state(
        active_agents=0,
        override={"state": "bogus", "expires_at": 101},
        now=100,
    ) == "idle"


def test_transition_plan_debounces_and_enforces_cooldown() -> None:
    first = MODULE.transition_plan(
        current="idle",
        desired="working",
        pending_state=None,
        pending_since=None,
        last_applied_at=0,
        now=10,
        debounce_seconds=3,
        cooldown_seconds=30,
    )
    assert first == {"apply": None, "pending_state": "working", "pending_since": 10}

    ready = MODULE.transition_plan(
        current="idle",
        desired="working",
        pending_state="working",
        pending_since=10,
        last_applied_at=0,
        now=13,
        debounce_seconds=3,
        cooldown_seconds=30,
    )
    assert ready["apply"] == "working"

    cooled = MODULE.transition_plan(
        current="idle",
        desired="working",
        pending_state="working",
        pending_since=10,
        last_applied_at=12,
        now=20,
        debounce_seconds=3,
        cooldown_seconds=30,
    )
    assert cooled["apply"] is None


def test_transition_plan_deduplicates_current_state() -> None:
    assert MODULE.transition_plan(
        current="working",
        desired="working",
        pending_state="idle",
        pending_since=1,
        last_applied_at=2,
        now=100,
        debounce_seconds=3,
        cooldown_seconds=30,
    ) == {"apply": None, "pending_state": None, "pending_since": None}


def test_discord_rename_reads_back_exact_identity_and_structure() -> None:
    calls: list[tuple[str, str, dict | None]] = []
    responses = iter([
        (200, {}, {"id": "42", "name": "operator・working", "parent_id": "7", "position": 3}),
        (200, {}, {"id": "42", "name": "operator・working", "parent_id": "7", "position": 3}),
    ])

    def request(method: str, path: str, payload: dict | None):
        calls.append((method, path, payload))
        return next(responses)

    client = MODULE.DiscordClient("secret", request=request, sleep=lambda _: None)
    readback = client.rename_and_verify(
        channel_id="42",
        desired="operator・working",
        baseline={"id": "42", "parent_id": "7", "position": 3},
    )

    assert readback["name"] == "operator・working"
    assert calls == [
        ("PATCH", "/channels/42", {"name": "operator・working"}),
        ("GET", "/channels/42", None),
    ]


def test_discord_client_surfaces_full_retry_after_without_retry_loop() -> None:
    calls = 0

    def request(*_):
        nonlocal calls
        calls += 1
        return (429, {"Retry-After": "583.25"}, {"retry_after": 583.25})

    client = MODULE.DiscordClient("secret", request=request)
    with pytest.raises(MODULE.DiscordRateLimited) as caught:
        client.rename_and_verify(
            channel_id="42",
            desired="operator・working",
            baseline={"id": "42", "parent_id": "7", "position": 3},
        )
    assert caught.value.retry_after == 583.25
    assert calls == 1


def test_discord_readback_rejects_parent_or_position_drift() -> None:
    responses = iter([
        (200, {}, {"id": "42", "name": "operator・working", "parent_id": "7", "position": 3}),
        (200, {}, {"id": "42", "name": "operator・working", "parent_id": "99", "position": 3}),
    ])
    client = MODULE.DiscordClient("secret", request=lambda *_: next(responses), sleep=lambda _: None)
    with pytest.raises(RuntimeError, match="Discord readback invariant failure"):
        client.rename_and_verify(
            channel_id="42",
            desired="operator・working",
            baseline={"id": "42", "parent_id": "7", "position": 3},
        )


def test_projector_debounces_writes_and_deduplicates_repeated_runtime_state(tmp_path: Path) -> None:
    gateway = tmp_path / "gateway_state.json"
    gateway.write_text('{"active_agents": 1}', encoding="utf-8")
    calls: list[str] = []

    class Client:
        def get_channel(self, channel_id: str) -> dict:
            return {"id": channel_id, "name": "operator・idle", "parent_id": "7", "position": 3}

        def rename_and_verify(self, *, channel_id: str, desired: str, baseline: dict) -> dict:
            calls.append(desired)
            return {**baseline, "name": desired}

    projector = MODULE.ChannelStateProjector(
        client=Client(),
        channel_id="42",
        base_name="operator",
        baseline={"id": "42", "parent_id": "7", "position": 3},
        gateway_state_path=gateway,
        override_path=tmp_path / "override.json",
        state_path=tmp_path / "state.json",
        debounce_seconds=3,
        cooldown_seconds=30,
    )
    projector.initialize(now=10)
    assert projector.tick(now=10) is None
    assert projector.tick(now=13) == "operator・working"
    assert projector.tick(now=100) is None
    assert calls == ["operator・working"]


def test_projector_persists_rate_limit_and_does_not_retry_before_deadline(tmp_path: Path) -> None:
    gateway = tmp_path / "gateway_state.json"
    gateway.write_text('{"active_agents": 0}', encoding="utf-8")
    calls: list[str] = []

    class Client:
        def get_channel(self, channel_id: str) -> dict:
            return {"id": channel_id, "name": "operator・working", "parent_id": "7", "position": 3}

        def rename_and_verify(self, *, channel_id: str, desired: str, baseline: dict) -> dict:
            calls.append(desired)
            raise MODULE.DiscordRateLimited(583.25)

    state = tmp_path / "state.json"
    projector = MODULE.ChannelStateProjector(
        client=Client(),
        channel_id="42",
        base_name="operator",
        baseline={"id": "42", "parent_id": "7", "position": 3},
        gateway_state_path=gateway,
        override_path=tmp_path / "override.json",
        state_path=state,
        debounce_seconds=0,
        cooldown_seconds=0,
    )
    projector.initialize(now=100)
    assert projector.tick(now=100) is None
    assert projector.tick(now=101) is None
    assert MODULE._read_json(state)["rate_limited_until"] == 684.25
    assert projector.tick(now=600) is None
    assert calls == ["operator・idle"]


def test_projector_rejects_missing_or_malformed_gateway_state(tmp_path: Path) -> None:
    class Client:
        def get_channel(self, channel_id: str) -> dict:
            return {"id": channel_id, "name": "operator", "parent_id": "7", "position": 3}

    projector = MODULE.ChannelStateProjector(
        client=Client(),
        channel_id="42",
        base_name="operator",
        baseline={"id": "42", "parent_id": "7", "position": 3},
        gateway_state_path=tmp_path / "gateway_state.json",
        override_path=tmp_path / "override.json",
        state_path=tmp_path / "state.json",
    )
    with pytest.raises(RuntimeError, match="gateway state unavailable"):
        projector.tick(now=1)


def test_override_is_typed_expiring_and_atomic(tmp_path: Path) -> None:
    path = tmp_path / "override.json"
    MODULE.write_override(path, state="approval", ttl_seconds=60, now=100)
    assert MODULE._read_json(path) == {"expires_at": 160.0, "state": "approval"}
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="unsupported channel state"):
        MODULE.write_override(path, state="done", ttl_seconds=60, now=100)


def test_dotenv_token_loader_reads_only_exact_key(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "OTHER_TOKEN=nope\nDISCORD_BOT_TOKEN='expected'\nDISCORD_BOT_TOKEN_SUFFIX=nope\n",
        encoding="utf-8",
    )
    assert MODULE.load_dotenv_value(path, "DISCORD_BOT_TOKEN") == "expected"
    with pytest.raises(RuntimeError, match="missing required credential"):
        MODULE.load_dotenv_value(path, "MISSING")


def test_client_ignores_ambient_token_and_uses_exact_profile_env(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".env").write_text("DISCORD_BOT_TOKEN=profile-token\n", encoding="utf-8")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "wrong-ambient-token")
    client = MODULE._client(tmp_path)
    assert client._token == "profile-token"


def test_malformed_retry_after_uses_safe_fallback() -> None:
    client = MODULE.DiscordClient(
        "secret",
        request=lambda *_: (429, {"Retry-After": "not-a-number"}, {"retry_after": "bad"}),
    )
    with pytest.raises(MODULE.DiscordRateLimited) as caught:
        client.get_channel("42")
    assert caught.value.retry_after == 1.0


def test_cli_parser_exposes_bounded_runtime_and_override_commands() -> None:
    parser = MODULE.build_parser()
    run = parser.parse_args([
        "run", "--channel-id", "42", "--base-name", "operator",
        "--parent-id", "7", "--position", "3",
    ])
    assert run.command == "run"
    assert run.poll_seconds == 2.0
    override = parser.parse_args(["set-state", "blocked", "--ttl", "60"])
    assert override.state == "blocked"
    with pytest.raises(SystemExit):
        parser.parse_args(["set-state", "done"])


def test_fleet_manifest_has_exact_five_id_routed_targets() -> None:
    manifest = Path(__file__).parents[1] / "overlay" / "config" / "discord-channel-state.json"
    targets = MODULE.load_targets(manifest)
    assert [(row["key"], row["base_name"], row["channel_id"]) for row in targets] == [
        ("operator", "operator", "1541820137148260432"),
        ("agentik", "agentik", "1541820106479501322"),
        ("mission", "mission", "1541814383007764570"),
        ("private", "private", "1541820077278503072"),
        ("collective", "discord", "1541847685680603387"),
    ]
    assert len({row["channel_id"] for row in targets}) == 5


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("key", "../../escape"),
        ("user", "operator\t--bad"),
        ("channel_id", "not-a-snowflake"),
        ("parent_id", "1541820192454082580\nInjected=true"),
        ("base_name", "operator\t--bad"),
        ("hermes_home", "/home/operator/.hermes-escape"),
    ],
)
def test_manifest_rejects_unsafe_identifiers_and_paths(tmp_path: Path, field: str, value: str) -> None:
    source = Path(__file__).parents[1] / "overlay" / "config" / "discord-channel-state.json"
    payload = __import__("json").loads(source.read_text(encoding="utf-8"))
    payload["targets"][0][field] = value
    manifest = tmp_path / "manifest.json"
    manifest.write_text(__import__("json").dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        MODULE.load_targets(manifest)


def test_rendered_unit_is_profile_isolated_and_never_contains_a_token() -> None:
    target = {
        "key": "collective",
        "user": "mission",
        "hermes_home": "/home/mission/.hermes/profiles/collective",
        "channel_id": "1541847685680603387",
        "base_name": "discord",
        "parent_id": "1541820192454082580",
        "position": 7,
    }
    unit = MODULE.render_unit(target, script_path="/usr/local/lib/agk-terminal/scripts/station_discord_channel_state.py")
    assert "HERMES_HOME=/home/mission/.hermes/profiles/collective" in unit
    assert "--channel-id 1541847685680603387" in unit
    assert "DISCORD_BOT_TOKEN" not in unit
    assert "Restart=on-failure" in unit


def test_installer_copy_is_safe_when_executed_from_installed_path(tmp_path: Path) -> None:
    installed = tmp_path / "station_discord_channel_state.py"
    installed.write_text("stable\n", encoding="utf-8")
    assert INSTALLER.copy_file(installed, installed) is False
    assert installed.read_text(encoding="utf-8") == "stable\n"
