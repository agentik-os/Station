import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "overlay" / "scripts" / "station_durability.py"


def load_module():
    spec = importlib.util.spec_from_file_location("station_durability", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_atomic_report_write_does_not_follow_symlinks_and_is_private(tmp_path, monkeypatch):
    module = load_module()
    output = tmp_path / "report.json"
    victim = tmp_path / "victim.txt"
    victim.write_text("unchanged")
    temporary = tmp_path / ".report.json.tmp"
    temporary.symlink_to(victim)

    module.write_json(output, {"status": "PASS"})

    assert victim.read_text() == "unchanged"
    assert json.loads(output.read_text()) == {"status": "PASS"}
    assert output.stat().st_mode & 0o777 == 0o600

    victim.chmod(0o644)

    def forbid_path_chmod(*args, **kwargs):
        raise AssertionError("write_json must set mode through the pinned temporary descriptor")

    monkeypatch.setattr(module.os, "chmod", forbid_path_chmod)
    module.write_json(output, {"status": "PASS"})
    assert victim.stat().st_mode & 0o777 == 0o644


def test_atomic_report_write_rejects_symlinked_parent(tmp_path):
    module = load_module()
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(OSError):
        module.write_json(linked_parent / "report.json", {"status": "PASS"})

    assert not (real_parent / "report.json").exists()


def test_atomic_report_write_rejects_raced_ancestor_into_audited_tree(tmp_path):
    module = load_module()
    audited = tmp_path / "audited"
    (audited / "leaf").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "ancestor" / "leaf").mkdir(parents=True)
    output = outside / "ancestor" / "leaf" / "report.json"

    (outside / "ancestor").rename(outside / "parked")
    (outside / "ancestor").symlink_to(audited, target_is_directory=True)

    with pytest.raises((OSError, ValueError)):
        module.write_json(output, {"status": "PASS"}, forbidden_roots=(audited,))
    assert not (audited / "leaf" / "report.json").exists()


def test_canonical_project_context_and_template_define_durable_layers():
    context = (ROOT / ".hermes.md").read_text()
    template = (ROOT / "docs" / "templates" / "OPERATIVE_SYSTEM_CONTEXT.md").read_text()
    for marker in (
        "Ownership and trust boundaries",
        "Verification and completion",
        "Deployment and rollback",
        "Memory, context, and Skills",
        "Fresh-session acceptance",
    ):
        assert marker in context
        assert marker in template
    assert "NEVER include API keys" in context
    assert "Completion Oracle" in context


def test_policy_declares_profile_cron_memory_execution_and_checkpoint_rules():
    policy = json.loads((ROOT / "overlay" / "config" / "station-durability-policy.json").read_text())
    assert policy["schema_version"] == 1
    assert policy["profiles"]["no_delete"] is True
    assert policy["memory"]["automatic_rewrite"] is False
    assert policy["cron"]["retirement_requires_human"] is True
    assert policy["execution"]["pilot"]["network"] == "none"
    assert policy["execution"]["pilot"]["transport"] == "stdin_stdout"
    assert policy["execution"]["pilot"]["host_mounts"] is False
    assert policy["checkpoints"]["global_default"] is False
    assert policy["checkpoints"]["development_opt_in"] is True


def test_profile_audit_is_metadata_only_and_never_deletes(tmp_path):
    module = load_module()
    profiles = tmp_path / "profiles"
    durable = profiles / "builder-os"
    thin = profiles / "scratch-worker"
    for profile in (durable, thin):
        (profile / "skills").mkdir(parents=True)
        (profile / "cron").mkdir()
    (durable / "profile.yaml").write_text("display_name: Builder\n")
    (durable / "skills" / "one").mkdir()
    (thin / "cron" / "jobs.json").write_text('{"private_prompt":"must-not-leak"}')
    policy = {
        "profiles": {
            "no_delete": True,
            "classifications": {"builder-os": "durable"},
            "durable_signals": ["memories", "sessions", "cron"],
        }
    }

    report = module.audit_profiles(profiles, policy)

    by_id = {row["profile_id_sha256"]: row for row in report["profiles"]}
    builder = module.sha256_bytes(b"builder-os")
    scratch = module.sha256_bytes(b"scratch-worker")
    assert by_id[builder]["classification"] == "durable"
    assert by_id[scratch]["classification"] == "review"
    assert report["mutation_performed"] is False
    assert report["deletion_allowed"] is False
    serialized = json.dumps(report)
    assert "private_prompt" not in serialized
    assert "must-not-leak" not in serialized


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("Gareth prefers concise operational reports.", "keep_user"),
        ("Project Station uses pytest for verification.", "project_context"),
        ("When deploying a gateway, run the drain-safe verification workflow.", "skill_candidate"),
        ("Completed commit abcdef0123456789 last week.", "remove_candidate"),
        ("Discord channel 1541820137148260432 belongs to Operator.", "keep_memory"),
        ("Docker is available on this host.", "keep_memory"),
    ],
)
def test_memory_curator_proposes_without_exposing_or_rewriting(entry, expected):
    module = load_module()
    report = module.audit_memory_text(entry)
    assert report["automatic_rewrite"] is False
    assert report["entries"][0]["recommendation"] == expected
    assert "text" not in report["entries"][0]
    assert entry not in json.dumps(report)


def test_fresh_session_gate_requires_context_tools_artifact_checks_and_rollback(tmp_path):
    module = load_module()
    context = tmp_path / ".hermes.md"
    context.write_text("canonical rules")
    artifact = tmp_path / "output.json"
    artifact.write_text('{"status":"PASS"}\n')
    receipt = {
        "schema_version": 1,
        "fresh_session": True,
        "project_context": {"path": str(context), "sha256": module.sha256_file(context)},
        "skills_loaded": ["verified-builder"],
        "toolsets": ["file", "terminal"],
        "artifact": {"path": str(artifact), "sha256": module.sha256_file(artifact)},
        "checks": [{"name": "schema", "status": "PASS", "evidence": "exit=0"}],
        "rollback": {"available": True, "procedure": "restore backup"},
        "delivery": {"required": False, "verified": True},
    }
    assert module.validate_fresh_session_receipt(receipt) == []

    receipt["artifact"]["sha256"] = "b" * 64
    errors = module.validate_fresh_session_receipt(receipt)
    assert any("artifact.sha256" in error for error in errors)
    receipt["artifact"]["sha256"] = module.sha256_file(artifact)

    receipt["fresh_session"] = False
    receipt["checks"][0]["status"] = "FAIL"
    errors = module.validate_fresh_session_receipt(receipt)
    assert any("fresh_session" in error for error in errors)
    assert any("checks" in error for error in errors)


def test_cron_registry_reconciles_without_prompts_or_mutation():
    module = load_module()
    jobs = [
        {
            "id": "a1b2c3d4e5f6",
            "name": "Daily report",
            "schedule": {"display": "0 8 * * *", "kind": "cron", "expr": "0 8 * * *"},
            "state": "scheduled",
            "enabled": True,
            "last_status": "ok",
            "last_run_at": "2026-08-31T08:00:00+00:00",
            "deliver": "origin",
            "prompt": "secret workflow text",
            "skills": ["research"],
            "enabled_toolsets": ["web", "file"],
        },
        {
            "job_id": "deadbeefcafe",
            "name": "Old one-shot",
            "schedule": "once",
            "state": "completed",
            "enabled": False,
            "last_status": "ok",
            "last_run_at": "2026-01-01T00:00:00+00:00",
            "deliver": "local",
        },
    ]
    report = module.reconcile_cron_jobs(
        jobs,
        now=datetime(2026, 8, 31, tzinfo=timezone.utc),
        retirement_days=30,
    )
    assert report["mutation_performed"] is False
    assert report["retirement_requires_human"] is True
    assert report["summary"] == {"active": 1, "paused": 0, "completed": 1, "attention": 0}
    old = next(row for row in report["jobs"] if row["job_id"] == "deadbeefcafe")
    assert old["retirement_candidate"] is True
    active = next(row for row in report["jobs"] if row["job_id"] == "a1b2c3d4e5f6")
    assert active["schedule_kind"] == "cron"
    assert active["skill_count"] == 1
    assert active["toolset_count"] == 2
    assert "prompt" not in json.dumps(report)
    assert "secret workflow text" not in json.dumps(report)


def test_cron_registry_redacts_secret_like_metadata():
    module = load_module()
    github_like = "gh" + "p_" + "A" * 36
    aws_like = "AKIA" + "A" * 16
    slack_like = "xoxb-" + "1" * 12 + "-" + "A" * 24
    report = module.reconcile_cron_jobs(
        [{
            "id": "safe-id password=SUPERSECRET123",
            "name": "report password=SUPERSECRET123",
            "schedule": {"kind": "cron", "token": "SUPERSECRET123", "display": "daily secret=SUPERSECRET123"},
            "deliver": {
                "url": "https://user:SUPERSECRET123@example.invalid/path?api_key=SUPERSECRET123",
                "secret": slack_like,
                "nested": {"clientSecret": "LEAKME_SUPERSECRET_987654321"},
                "credential=KEY_NAME_SECRET_123456789": "ordinary-value",
            },
            "last_status": "failed client_secret=SUPERSECRET123 DISCORD_BOT_TOKEN=SUPERSECRET123",
            "skills": [f"skill password=SUPERSECRET123 {github_like}"],
            "enabled_toolsets": [f"file {aws_like}"],
            "unknown": {
                "api%5Fkey": "PERCENT_KEY_SECRET_123456789",
                "nested": "next%3Ftoken%3DPERCENT_ASSIGNMENT_SECRET_123456789",
                "pem": "-----BEGIN PRIVATE KEY-----\\nPEM_SECRET_123456789\\n-----END PRIVATE KEY-----",
            },
            "state": "scheduled",
            "enabled": True,
        }]
    )
    serialized = json.dumps(report)
    assert "SUPERSECRET123" not in serialized
    assert github_like not in serialized
    assert aws_like not in serialized
    assert slack_like not in serialized
    assert "LEAKME_SUPERSECRET_987654321" not in serialized
    assert "KEY_NAME_SECRET_123456789" not in serialized
    assert "PERCENT_KEY_SECRET_123456789" not in serialized
    assert "PERCENT_ASSIGNMENT_SECRET_123456789" not in serialized
    assert "PEM_SECRET_123456789" not in serialized
    assert "clientSecret" not in serialized
    assert "name" not in report["jobs"][0]
    assert "delivery" not in report["jobs"][0]
    assert "job_id_sha256" in report["jobs"][0]
    assert report["sensitive_payloads_included"] is False


def test_cron_registry_handles_malformed_items_without_exposing_them():
    module = load_module()
    secret = "-----BEGIN PRIVATE KEY----- MALFORMED_SECRET_123456789"
    report = module.reconcile_cron_jobs([None, secret])

    assert report["summary"]["attention"] == 2
    assert all(row["source_valid"] is False for row in report["jobs"])
    assert secret not in json.dumps(report)


@pytest.mark.parametrize("command", ["memory-audit", "cron-audit"])
def test_audit_cli_refuses_output_aliasing_an_input(tmp_path, command):
    module = load_module()
    source = tmp_path / "source.json"
    if command == "memory-audit":
        source.write_text("durable memory text")
        argv = [command, "--memory-file", str(source), "--output", str(source)]
    else:
        source.write_text('{"jobs": []}\n')
        argv = [command, "--jobs-json", str(source), "--output", str(source)]
    before = source.read_bytes()

    assert module.main(argv) == 2
    assert source.read_bytes() == before


def test_profile_audit_refuses_output_inside_audited_tree(tmp_path):
    module = load_module()
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    policy = tmp_path / "policy.json"
    policy.write_text('{"profiles": {"no_delete": true}}\n')
    output = profiles / "report.json"

    assert module.main([
        "profile-audit", "--profiles-root", str(profiles), "--policy", str(policy), "--output", str(output)
    ]) == 2
    assert not output.exists()


def test_isolated_pilot_command_is_fail_closed_and_networkless(tmp_path):
    module = load_module()
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()
    command = module.build_isolated_pilot_command(input_dir, output_dir, image="python:3.11-slim")
    joined = " ".join(command)
    for expected in ("--network", "none", "--read-only", "--cap-drop", "ALL", "no-new-privileges"):
        assert expected in command
    assert str(input_dir.resolve()) not in command
    assert str(output_dir.resolve()) not in command
    assert "-v" not in command
    assert "-i" in command
    assert "--user" in command
    assert f"{os.getuid()}:{os.getgid()}" in command
    assert "--privileged" not in command
    assert "host" not in command
    assert "--rm" in command

    with pytest.raises(ValueError, match="temporary directory"):
        module.build_isolated_pilot_command(Path("/"), Path("/"), image="python:3.11-slim")

    nested = input_dir / "nested"
    nested.mkdir()
    with pytest.raises(ValueError, match="overlap"):
        module.build_isolated_pilot_command(input_dir, nested, image="python:3.11-slim")

    with pytest.raises(ValueError, match="image"):
        module.build_isolated_pilot_command(input_dir, output_dir, image="--privileged")


def test_checkpoint_development_command_is_opt_in_and_never_yolo(tmp_path):
    module = load_module()
    repo = tmp_path / "repo"
    repo.mkdir()
    query = tmp_path / "query.md"
    query.write_text("perform bounded work")
    command = module.build_checkpoint_dev_command(repo, query)
    assert command[:2] == ["hermes", "chat"]
    assert "--checkpoints" in command
    assert "--worktree" in command
    assert "--query-file" in command
    assert "--yolo" not in command


def test_station_cli_and_overlay_installer_expose_durability_tool():
    station = (ROOT / "bin" / "station").read_text()
    installer = (ROOT / "overlay" / "install.sh").read_text()
    operations = (ROOT / "docs" / "OPERATIONS.md").read_text()
    assert "durability)" in station
    assert "station_durability.py" in station
    assert "station_durability.py" in installer
    assert "station-durability-policy.json" in installer
    for command in ("profile-audit", "memory-audit", "cron-audit", "fresh-session-gate", "isolated-pilot", "checkpoint-command"):
        assert command in operations
