#!/usr/bin/env python3
"""Fail-closed durability audits for Station.

This module intentionally emits metadata and hashes, never profile-private file
contents, memory entry text, cron prompts, credentials, or connection strings.
All audit operations are read-only. Destructive profile/cron/memory actions are
outside this tool and require a separately reviewed owner-gated workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"\b[0-9a-f]{12,40}\b", re.IGNORECASE)
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}$")
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _open_directory_nofollow(path: Path) -> int:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    absolute = Path(os.path.abspath(path))
    descriptor = os.open("/", directory_flags)
    try:
        for part in absolute.parts[1:]:
            child = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _fd_is_within(directory_fd: int, root_fd: int) -> bool:
    root = os.fstat(root_fd)
    current_fd = os.dup(directory_fd)
    try:
        while True:
            current = os.fstat(current_fd)
            if (current.st_dev, current.st_ino) == (root.st_dev, root.st_ino):
                return True
            parent_fd = os.open("..", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0), dir_fd=current_fd)
            parent = os.fstat(parent_fd)
            if (parent.st_dev, parent.st_ino) == (current.st_dev, current.st_ino):
                os.close(parent_fd)
                return False
            os.close(current_fd)
            current_fd = parent_fd
    finally:
        os.close(current_fd)


def write_json(path: Path, payload: Any, *, forbidden_roots: Iterable[Path] = ()) -> None:
    directory_fd = _open_directory_nofollow(path.parent)
    temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
    descriptor = -1
    try:
        for root in forbidden_roots:
            root_fd = _open_directory_nofollow(root)
            try:
                if _fd_is_within(directory_fd, root_fd):
                    raise ValueError("audit output must not be inside an audited tree")
            finally:
                os.close(root_fd)
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(temporary_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _validate_audit_output(output: Path, *, read_inputs: Iterable[Path], audited_roots: Iterable[Path] = ()) -> None:
    resolved_output = output.resolve(strict=False)
    for source in read_inputs:
        resolved_source = source.resolve(strict=True)
        if resolved_output == resolved_source:
            raise ValueError("audit output must not alias an input")
    for root in audited_roots:
        resolved_root = root.resolve(strict=True)
        if resolved_output == resolved_root or resolved_root in resolved_output.parents:
            raise ValueError("audit output must not be inside an audited tree")


def _count_children(path: Path) -> int:
    try:
        return sum(1 for child in path.iterdir() if not child.name.startswith("."))
    except (FileNotFoundError, PermissionError):
        return 0


def audit_profiles(profiles_root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    rules = policy.get("profiles") or {}
    classifications = rules.get("classifications") or {}
    rows: list[dict[str, Any]] = []
    if profiles_root.exists():
        for profile in sorted((p for p in profiles_root.iterdir() if p.is_dir()), key=lambda p: p.name):
            counts = {
                name: _count_children(profile / name)
                for name in ("skills", "cron", "memories", "sessions", "plugins")
            }
            rows.append(
                {
                    "profile_id_sha256": sha256_bytes(profile.name.encode("utf-8")),
                    "classification": classifications.get(profile.name, "review")
                    if classifications.get(profile.name) in {"durable", "durable_review", "ownership_review", "review"}
                    else "review",
                    "metadata": {
                        "profile_manifest_present": (profile / "profile.yaml").is_file(),
                        "config_present": (profile / "config.yaml").is_file(),
                        "soul_present": (profile / "SOUL.md").is_file(),
                        "counts": counts,
                    },
                    "action": "retain_pending_review",
                }
            )
    summary: dict[str, int] = {}
    for row in rows:
        key = row["classification"]
        summary[key] = summary.get(key, 0) + 1
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profiles_root": str(profiles_root),
        "metadata_only": True,
        "mutation_performed": False,
        "deletion_allowed": False if rules.get("no_delete", True) else False,
        "summary": summary,
        "profiles": rows,
    }


def _memory_entries(text: str) -> Iterable[tuple[int, str]]:
    normalized = text.replace("\r\n", "\n")
    chunks = re.split(r"(?:^|\n)§(?:\n|$)", normalized)
    index = 0
    for chunk in chunks:
        entry = chunk.strip()
        if not entry:
            continue
        index += 1
        yield index, entry


def _memory_recommendation(entry: str) -> tuple[str, str]:
    lowered = entry.lower()
    commit_signal = bool(COMMIT_RE.search(entry)) and any(word in lowered for word in ("commit", " sha", "head "))
    if commit_signal or any(word in lowered for word in ("completed-work", "completed commit", "last week", "yesterday")):
        return "remove_candidate", "stale_or_completed_work_signal"
    if any(word in lowered for word in ("prefers", "preference", "gareth wants", "gareth:", "user wants")):
        return "keep_user", "stable_user_preference_signal"
    if lowered.startswith("when ") or any(word in lowered for word in (" workflow", " procedure", " steps:", "run the ")):
        return "skill_candidate", "repeatable_procedure_signal"
    if any(word in lowered for word in ("project ", "repository", "repo ", "codebase", "pytest", "architecture")):
        return "project_context", "project_specific_signal"
    return "keep_memory", "durable_environment_or_lesson_signal"


def audit_memory_text(text: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for index, entry in _memory_entries(text):
        recommendation, reason = _memory_recommendation(entry)
        rows.append(
            {
                "index": index,
                "sha256": sha256_bytes(entry.encode("utf-8")),
                "bytes": len(entry.encode("utf-8")),
                "recommendation": recommendation,
                "reason": reason,
            }
        )
    counts: dict[str, int] = {}
    for row in rows:
        key = row["recommendation"]
        counts[key] = counts.get(key, 0) + 1
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "automatic_rewrite": False,
        "entry_text_included": False,
        "summary": counts,
        "entries": rows,
    }


def validate_fresh_session_receipt(receipt: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if receipt.get("schema_version") != 1:
        errors.append("schema_version must equal 1")
    if receipt.get("fresh_session") is not True:
        errors.append("fresh_session must be true")

    context = receipt.get("project_context")
    if not isinstance(context, dict):
        errors.append("project_context must be an object")
    else:
        path_value = context.get("path")
        digest = context.get("sha256")
        if not isinstance(path_value, str) or not path_value:
            errors.append("project_context.path is required")
        elif not Path(path_value).is_file():
            errors.append("project_context.path does not exist")
        elif not isinstance(digest, str) or sha256_file(Path(path_value)) != digest:
            errors.append("project_context.sha256 does not match")

    for field in ("skills_loaded", "toolsets"):
        value = receipt.get(field)
        if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
            errors.append(f"{field} must be a non-empty string list")

    artifact = receipt.get("artifact")
    if not isinstance(artifact, dict):
        errors.append("artifact must contain path and sha256")
    else:
        artifact_path = artifact.get("path")
        artifact_digest = artifact.get("sha256")
        if not isinstance(artifact_path, str) or not artifact_path:
            errors.append("artifact.path is required")
        elif not Path(artifact_path).is_file():
            errors.append("artifact.path does not exist")
        elif not isinstance(artifact_digest, str) or not HEX64_RE.fullmatch(artifact_digest):
            errors.append("artifact.sha256 must be a lowercase SHA-256 digest")
        elif sha256_file(Path(artifact_path)) != artifact_digest:
            errors.append("artifact.sha256 does not match")

    checks = receipt.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("checks must be non-empty")
    elif any(not isinstance(check, dict) or check.get("status") != "PASS" or not check.get("evidence") for check in checks):
        errors.append("checks must all have PASS status and evidence")

    rollback = receipt.get("rollback")
    if not isinstance(rollback, dict) or rollback.get("available") is not True or not rollback.get("procedure"):
        errors.append("rollback must be available with a procedure")

    delivery = receipt.get("delivery")
    if not isinstance(delivery, dict) or delivery.get("verified") is not True:
        errors.append("delivery must be explicitly verified")
    return errors


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _metadata_digest(value: Any) -> str:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        encoded = type(value).__name__.encode("ascii", errors="replace")
    return sha256_bytes(encoded)


def _schedule_kind(value: Any) -> str:
    kind = value.get("kind") if isinstance(value, dict) else value
    return kind if kind in {"at", "cron", "every", "interval", "once"} else "other"


def _string_count(value: Any) -> int:
    return sum(isinstance(item, str) for item in value) if isinstance(value, list) else 0


def reconcile_cron_jobs(
    jobs: list[Any],
    *,
    now: datetime | None = None,
    retirement_days: int = 30,
) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rows: list[dict[str, Any]] = []
    summary = {"active": 0, "paused": 0, "completed": 0, "attention": 0}
    for source in jobs:
        source_valid = isinstance(source, dict)
        job = source if source_valid else {}
        state = job.get("state")
        enabled = job.get("enabled") is True
        if source_valid and enabled and state == "scheduled":
            lifecycle = "active"
        elif source_valid and state == "paused":
            lifecycle = "paused"
        elif source_valid and state == "completed":
            lifecycle = "completed"
        else:
            lifecycle = "attention"
        summary[lifecycle] += 1
        last_run = _parse_time(job.get("last_run_at"))
        age_days = (current - last_run).days if last_run and current >= last_run else None
        retirement_candidate = lifecycle == "completed" and age_days is not None and age_days >= retirement_days
        identifier = job.get("job_id") or job.get("id")
        rows.append(
            {
                "job_id": identifier if isinstance(identifier, str) and JOB_ID_RE.fullmatch(identifier) else "[REDACTED]",
                "job_id_sha256": _metadata_digest(identifier),
                "source_valid": source_valid,
                "lifecycle": lifecycle,
                "enabled": enabled,
                "schedule_kind": _schedule_kind(job.get("schedule")),
                "skill_count": _string_count(job.get("skills")),
                "toolset_count": _string_count(job.get("enabled_toolsets")),
                "age_days": age_days,
                "retirement_candidate": retirement_candidate,
                "action": "human_review_before_remove" if retirement_candidate else "retain",
            }
        )
    return {
        "schema_version": 1,
        "generated_at": current.isoformat(),
        "mutation_performed": False,
        "sensitive_payloads_included": False,
        "retirement_requires_human": True,
        "summary": summary,
        "jobs": rows,
    }


_PILOT_CODE = """import hashlib,json,sys
raw=sys.stdin.buffer.read()
data=json.loads(raw)
items=data.get('items',[])
result={'schema_version':1,'item_count':len(items),'input_sha256':hashlib.sha256(raw).hexdigest(),'network':'none','mutation_scope':'/output'}
sys.stdout.write(json.dumps(result,sort_keys=True)+'\\n')
"""


def build_isolated_pilot_command(input_dir: Path, output_dir: Path, *, image: str) -> list[str]:
    if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
        raise ValueError("pilot image must be an explicit image reference, not a Docker option")
    temp_root = Path(tempfile.gettempdir()).resolve()
    raw_input = input_dir
    raw_output = output_dir
    if raw_input.is_symlink() or raw_output.is_symlink():
        raise ValueError("pilot directories must not be symlinks")
    try:
        resolved_input = raw_input.resolve(strict=True)
        resolved_output = raw_output.resolve(strict=True)
    except FileNotFoundError as error:
        raise ValueError("pilot directories must exist") from error
    if not resolved_input.is_dir() or not resolved_output.is_dir():
        raise ValueError("pilot paths must be directories")
    if resolved_input == temp_root or resolved_output == temp_root:
        raise ValueError("pilot directories must be bounded children of the temporary directory")
    if temp_root not in resolved_input.parents or temp_root not in resolved_output.parents:
        raise ValueError("pilot directories must be under the temporary directory")
    if resolved_input == resolved_output or resolved_input in resolved_output.parents or resolved_output in resolved_input.parents:
        raise ValueError("pilot input and output directories must not overlap")
    if resolved_output.stat().st_uid != os.getuid():
        raise ValueError("pilot output directory must be owned by the invoking user")
    return [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "64",
        "--memory",
        "256m",
        "--cpus",
        "1",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=16m",
        image,
        "python3",
        "-I",
        "-c",
        _PILOT_CODE,
    ]


def build_checkpoint_dev_command(repo: Path, query_file: Path) -> list[str]:
    return [
        "hermes",
        "chat",
        "--checkpoints",
        "--worktree",
        "--query-file",
        str(query_file.resolve()),
        "--in",
        str(repo.resolve()),
    ]


def _command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    profile = subparsers.add_parser("profile-audit")
    profile.add_argument("--profiles-root", type=Path, required=True)
    profile.add_argument("--policy", type=Path, required=True)
    profile.add_argument("--output", type=Path, required=True)

    memory = subparsers.add_parser("memory-audit")
    memory.add_argument("--memory-file", type=Path, required=True)
    memory.add_argument("--output", type=Path, required=True)

    cron = subparsers.add_parser("cron-audit")
    cron.add_argument("--jobs-json", type=Path, required=True)
    cron.add_argument("--output", type=Path, required=True)
    cron.add_argument("--retirement-days", type=int, default=30)

    gate = subparsers.add_parser("fresh-session-gate")
    gate.add_argument("receipt", type=Path)

    pilot = subparsers.add_parser("isolated-pilot")
    pilot.add_argument("--input-dir", type=Path, required=True)
    pilot.add_argument("--output-dir", type=Path, required=True)
    pilot.add_argument("--image", default="python:3.11-slim")
    pilot.add_argument("--execute", action="store_true")
    pilot.add_argument("--sudo", action="store_true")

    checkpoint = subparsers.add_parser("checkpoint-command")
    checkpoint.add_argument("--repo", type=Path, required=True)
    checkpoint.add_argument("--query-file", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _command_parser().parse_args(argv)
    if args.command == "profile-audit":
        try:
            _validate_audit_output(args.output, read_inputs=(args.policy,), audited_roots=(args.profiles_root,))
            report = audit_profiles(args.profiles_root, load_json(args.policy))
            write_json(args.output, report, forbidden_roots=(args.profiles_root,))
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
    if args.command == "memory-audit":
        try:
            _validate_audit_output(args.output, read_inputs=(args.memory_file,), audited_roots=(args.memory_file.parent,))
            report = audit_memory_text(args.memory_file.read_text(encoding="utf-8"))
            write_json(args.output, report, forbidden_roots=(args.memory_file.parent,))
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
    if args.command == "cron-audit":
        try:
            _validate_audit_output(args.output, read_inputs=(args.jobs_json,), audited_roots=(args.jobs_json.parent,))
            payload = load_json(args.jobs_json)
            jobs = payload.get("jobs") if isinstance(payload, dict) else payload
            if not isinstance(jobs, list):
                raise ValueError("jobs JSON must be a list or an object with jobs")
            report = reconcile_cron_jobs(jobs, retirement_days=args.retirement_days)
            write_json(args.output, report, forbidden_roots=(args.jobs_json.parent,))
        except (OSError, ValueError) as error:
            print(str(error), file=sys.stderr)
            return 2
        return 0
    if args.command == "fresh-session-gate":
        errors = validate_fresh_session_receipt(load_json(args.receipt))
        print(json.dumps({"valid": not errors, "errors": errors}, sort_keys=True))
        return 0 if not errors else 2
    if args.command == "isolated-pilot":
        command = build_isolated_pilot_command(args.input_dir, args.output_dir, image=args.image)
        if args.sudo:
            command.insert(0, "sudo")
        if not args.execute:
            print(json.dumps(command))
            return 0
        input_path = args.input_dir.resolve(strict=True) / "input.json"
        try:
            input_bytes = input_path.read_bytes()
            source = json.loads(input_bytes)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return 2
        completed = subprocess.run(command, input=input_bytes, capture_output=True, check=False)
        if completed.returncode != 0:
            return completed.returncode
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return 2
        expected = {
            "schema_version": 1,
            "item_count": len(source.get("items", [])) if isinstance(source, dict) and isinstance(source.get("items", []), list) else 0,
            "input_sha256": sha256_bytes(input_bytes),
            "network": "none",
            "mutation_scope": "/output",
        }
        if result != expected:
            return 2
        write_json(args.output_dir.resolve(strict=True) / "result.json", result)
        return 0
    if args.command == "checkpoint-command":
        print(json.dumps(build_checkpoint_dev_command(args.repo, args.query_file)))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
