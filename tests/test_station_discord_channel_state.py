from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
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


@pytest.mark.parametrize(
    ("headers", "body", "expected"),
    [
        ({"retry-after": "583.25"}, {}, 583.25),
        ({"Retry-After": "invalid"}, {"retry_after": 583.25}, 583.25),
        ({"Retry-After": "Infinity"}, {}, 1.0),
        ({}, {"retry_after": "NaN"}, 1.0),
        ({"Retry-After": "-1"}, {}, 1.0),
    ],
)
def test_retry_after_is_case_insensitive_finite_and_falls_back_to_body(
    headers: dict[str, str], body: dict, expected: float
) -> None:
    client = MODULE.DiscordClient("secret", request=lambda *_: (429, headers, body))
    with pytest.raises(MODULE.DiscordRateLimited) as caught:
        client.get_channel("42")
    assert caught.value.retry_after == expected


def test_retry_after_non_object_json_body_uses_bounded_fallback() -> None:
    client = MODULE.DiscordClient("secret", request=lambda *_: (429, {}, []))
    with pytest.raises(MODULE.DiscordRateLimited) as caught:
        client.get_channel("42")
    assert caught.value.retry_after == 1.0


def test_retry_after_oversized_numeric_value_uses_bounded_fallback() -> None:
    oversized = 10**4000
    assert MODULE._retry_after_seconds({}, {"retry_after": oversized}) == 1.0
    assert MODULE._retry_after_seconds({"Retry-After": oversized}, {}) == 1.0


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


def test_units_are_installed_only_in_root_controlled_systemd_search_path() -> None:
    target = {"key": "agentik", "user": "agentik"}
    assert INSTALLER.unit_path(target) == Path("/etc/systemd/user/station-discord-channel-state-agentik.service")
    assert INSTALLER.legacy_unit_path(target) == Path(
        "/home/agentik/.config/systemd/user/station-discord-channel-state-agentik.service"
    )


def test_atomic_install_replaces_hostile_symlink_without_touching_victim(tmp_path: Path) -> None:
    staging = tmp_path / "root-staging"
    staging.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.write_text("replacement\n", encoding="utf-8")
    victim = tmp_path / "victim"
    victim.write_text("do-not-touch\n", encoding="utf-8")
    destination = tmp_path / "unit.service"
    destination.symlink_to(victim)

    INSTALLER.atomic_install_file(
        source,
        destination,
        staging_dir=staging,
        mode=0o644,
        uid=os.getuid(),
        gid=os.getgid(),
    )

    assert not destination.is_symlink()
    assert destination.read_text(encoding="utf-8") == "replacement\n"
    assert victim.read_text(encoding="utf-8") == "do-not-touch\n"


def test_atomic_install_rejects_symlinked_parent_without_redirected_write(tmp_path: Path) -> None:
    staging = tmp_path / "root-staging"
    staging.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.write_text("replacement\n", encoding="utf-8")
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    parent = tmp_path / "user-systemd"
    parent.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(OSError):
        INSTALLER.atomic_install_file(
            source,
            parent / "unit.service",
            staging_dir=staging,
            mode=0o644,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert not (redirected / "unit.service").exists()


def test_atomic_install_detects_destination_parent_replacement(tmp_path: Path, monkeypatch) -> None:
    staging = tmp_path / "root-staging"
    staging.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.write_text("replacement\n", encoding="utf-8")
    parent = tmp_path / "user-systemd"
    parent.mkdir(mode=0o700)
    destination = parent / "unit.service"
    original_replace = INSTALLER.os.replace
    moved_parent = tmp_path / "renamed-away"

    def replace_after_parent_swap(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
        parent.rename(moved_parent)
        parent.mkdir(mode=0o700)
        destination.write_text("attacker-controlled\n", encoding="utf-8")
        return original_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(INSTALLER.os, "replace", replace_after_parent_swap)
    with pytest.raises(RuntimeError, match="destination parent changed"):
        INSTALLER.atomic_install_file(
            source,
            destination,
            staging_dir=staging,
            mode=0o644,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert destination.read_text(encoding="utf-8") == "attacker-controlled\n"


def test_atomic_install_closes_descriptors_when_parent_open_fails(tmp_path: Path) -> None:
    staging = tmp_path / "root-staging"
    staging.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.write_text("replacement\n", encoding="utf-8")
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    parent = tmp_path / "user-systemd"
    parent.symlink_to(redirected, target_is_directory=True)
    before = len(list(Path("/proc/self/fd").iterdir()))

    for _ in range(20):
        with pytest.raises(OSError):
            INSTALLER.atomic_install_file(
                source,
                parent / "unit.service",
                staging_dir=staging,
                mode=0o644,
                uid=os.getuid(),
                gid=os.getgid(),
            )

    assert len(list(Path("/proc/self/fd").iterdir())) == before


def test_atomic_install_preserves_timestamp_and_extended_attributes(tmp_path: Path) -> None:
    staging = tmp_path / "root-staging"
    staging.mkdir(mode=0o700)
    source = tmp_path / "source"
    source.write_text("original\n", encoding="utf-8")
    original_ns = 1_650_000_000_123_456_789
    os.utime(source, ns=(original_ns, original_ns))
    try:
        os.setxattr(source, "user.agk-test", b"preserved")
    except OSError as exc:
        pytest.skip(f"filesystem does not support user xattrs: {exc}")
    destination = tmp_path / "restored"

    INSTALLER.atomic_install_file(
        source,
        destination,
        staging_dir=staging,
        mode=0o640,
        uid=os.getuid(),
        gid=os.getgid(),
        preserve_metadata=True,
    )

    assert destination.stat().st_mtime_ns == original_ns
    assert os.getxattr(destination, "user.agk-test") == b"preserved"


def test_trusted_backup_root_rejects_symlink_and_writable_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        INSTALLER.ensure_trusted_directory(linked, create=False, trusted_ancestor=tmp_path)

    writable = tmp_path / "writable"
    writable.mkdir(mode=0o777)
    writable.chmod(0o777)
    with pytest.raises(PermissionError):
        INSTALLER.ensure_trusted_directory(writable, create=False, trusted_ancestor=tmp_path)


def test_install_failure_restores_all_files_and_service_state(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.py"
    source.write_text("new-projector\n", encoding="utf-8")
    installed = tmp_path / "installed.py"
    installed.write_text("old-projector\n", encoding="utf-8")
    installed.chmod(0o4755)
    backup_root = tmp_path / "backups"
    units = [tmp_path / "one.service", tmp_path / "two.service"]
    units[0].write_text("old-one\n", encoding="utf-8")
    units[1].write_text("old-two\n", encoding="utf-8")
    units[0].chmod(0o2750)
    units[1].chmod(0o1755)
    original_ns = 1_650_000_000_123_456_789
    os.utime(installed, ns=(original_ns, original_ns))
    for unit in units:
        os.utime(unit, ns=(original_ns, original_ns))
    targets = [
        {"key": "one", "user": "operator"},
        {"key": "two", "user": "operator"},
    ]
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(INSTALLER.os, "geteuid", lambda: 0)
    monkeypatch.setattr(INSTALLER, "PROJECTOR_SOURCE", source)
    monkeypatch.setattr(INSTALLER, "PROJECTOR_INSTALLED", installed)
    monkeypatch.setattr(INSTALLER, "INSTALL_ROOT", tmp_path)
    monkeypatch.setattr(INSTALLER, "BACKUP_ROOT", backup_root)
    monkeypatch.setattr(
        INSTALLER,
        "ensure_trusted_directory",
        lambda path, **_: Path(path).mkdir(mode=0o700, parents=True, exist_ok=True),
    )
    monkeypatch.setattr(INSTALLER.MODULE, "load_targets", lambda _: targets)
    monkeypatch.setattr(INSTALLER.MODULE, "render_unit", lambda target, **_: f"new-{target['key']}\n")
    monkeypatch.setattr(INSTALLER, "unit_path", lambda target: units[0 if target["key"] == "one" else 1])
    monkeypatch.setattr(
        INSTALLER.pwd,
        "getpwnam",
        lambda _user: type("Account", (), {"pw_uid": os.getuid(), "pw_gid": os.getgid()})(),
    )

    def systemctl(_user: str, *args: str, check: bool = True):
        calls.append(args)
        if args[:1] == ("is-enabled",):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:1] == ("is-active",):
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:1] == ("restart",) and args[-1] == units[1].name and check:
            raise subprocess.CalledProcessError(1, args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(INSTALLER, "user_systemctl", systemctl)
    with pytest.raises(subprocess.CalledProcessError):
        INSTALLER.install(tmp_path / "manifest.json")

    assert installed.read_text(encoding="utf-8") == "old-projector\n"
    assert [path.read_text(encoding="utf-8") for path in units] == ["old-one\n", "old-two\n"]
    assert installed.stat().st_mtime_ns == original_ns
    assert [path.stat().st_mtime_ns for path in units] == [original_ns, original_ns]
    assert stat.S_IMODE(installed.stat().st_mode) == 0o4755
    assert [stat.S_IMODE(path.stat().st_mode) for path in units] == [0o2750, 0o1755]
    assert ("enable", units[0].name) in calls
    assert ("start", units[0].name) in calls


def test_incomplete_rollback_keeps_failed_target_service_retryable(tmp_path: Path, monkeypatch) -> None:
    unit = tmp_path / "station-discord-channel-state-operator.service"
    unit.write_text("[Service]\n", encoding="utf-8")
    projector = tmp_path / "station_discord_channel_state.py"
    projector.write_text("# safe\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    target = {
        "key": "operator",
        "user": "operator",
        "hermes_home": "/home/operator/.hermes",
        "channel_id": "1541820137148260432",
        "base_name": "operator",
        "parent_id": "1541820192454082580",
        "position": 3,
    }
    monkeypatch.setattr(INSTALLER.os, "geteuid", lambda: 0)
    monkeypatch.setattr(INSTALLER.MODULE, "load_targets", lambda _: [target])
    monkeypatch.setattr(INSTALLER, "unit_path", lambda _: unit)
    monkeypatch.setattr(INSTALLER, "PROJECTOR_INSTALLED", projector)

    def systemctl(_user: str, *args: str, **_kwargs):
        calls.append(args)
        return __import__("subprocess").CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(INSTALLER, "user_systemctl", systemctl)
    monkeypatch.setattr(
        INSTALLER.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            __import__("subprocess").CalledProcessError(1, "restore")
        ),
    )
    result = INSTALLER.rollback(tmp_path / "manifest.json")
    assert result["status"] == "rollback-incomplete"
    assert unit.exists()
    assert ("enable", "--now", unit.name) in calls
