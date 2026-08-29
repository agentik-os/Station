#!/usr/bin/env python3
"""Conservative AGK disk maintenance with an explicit allowlist."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterable

TMP_PREFIXES = (
    "hermes_voice",
    "pytest-of-",
    "agk-test-",
    "agk-mission-",
    "station-audit-",
    "station-review-",
    "hermes-main-merge-preflight",
    "hermes-real-profile-preflight",
    "hermes-agk-baseline",
)
PROFILE_USERS = ("operator", "agentik", "mission", "private")
MAX_DELETE_BYTES = 20 * 1024**3


def _inside(path: Path, root: Path) -> bool:
    try:
        path.absolute().relative_to(root.absolute())
        return True
    except ValueError:
        return False


def safe_size(path: Path, root: Path) -> int:
    """Return non-symlink bytes without crossing filesystems or root."""
    try:
        if not _inside(path, root) or path.is_symlink():
            return 0
        root_dev = root.stat().st_dev
        st = path.lstat()
        if st.st_dev != root_dev:
            return 0
        if stat.S_ISREG(st.st_mode):
            return st.st_size
        if not stat.S_ISDIR(st.st_mode):
            return 0
        total = 0
        for base, dirs, files in os.walk(path, topdown=True, followlinks=False):
            base_path = Path(base)
            dirs[:] = [
                d for d in dirs
                if not (base_path / d).is_symlink()
                and (base_path / d).lstat().st_dev == root_dev
            ]
            for name in files:
                item = base_path / name
                try:
                    item_st = item.lstat()
                    if not stat.S_ISLNK(item_st.st_mode) and item_st.st_dev == root_dev:
                        total += item_st.st_size
                except OSError:
                    continue
        return total
    except OSError:
        return 0


def safe_remove(path: Path, root: Path, *, dry_run: bool) -> int:
    size = safe_size(path, root)
    if size <= 0 or path.is_symlink() or not _inside(path, root):
        return 0
    if dry_run:
        return size
    if path.is_dir():
        shutil.rmtree(path)
    elif path.is_file():
        path.unlink()
    return size


def collect_tmp_candidates(root: Path, *, now: float, older_than_days: int) -> list[Path]:
    cutoff = now - older_than_days * 86400
    result: list[Path] = []
    if not root.is_dir():
        return result
    for path in root.iterdir():
        try:
            if path.is_symlink() or not path.name.startswith(TMP_PREFIXES):
                continue
            if path.lstat().st_mtime < cutoff:
                result.append(path)
        except OSError:
            continue
    return sorted(result, key=lambda p: p.name)


def collect_old_files(root: Path, *, now: float, older_than_days: int) -> list[Path]:
    cutoff = now - older_than_days * 86400
    result: list[Path] = []
    if not root.is_dir() or root.is_symlink():
        return result
    root_dev = root.stat().st_dev
    for base, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base_path = Path(base)
        dirs[:] = [
            d for d in dirs
            if not (base_path / d).is_symlink()
            and (base_path / d).lstat().st_dev == root_dev
        ]
        for name in files:
            path = base_path / name
            try:
                st = path.lstat()
                if stat.S_ISREG(st.st_mode) and st.st_dev == root_dev and st.st_mtime < cutoff:
                    result.append(path)
            except OSError:
                continue
    return result


def remove_candidates(paths: Iterable[Path], root: Path, *, dry_run: bool, max_bytes: int) -> dict:
    candidates = list(dict.fromkeys(paths))
    planned = sum(safe_size(path, root) for path in candidates)
    if planned > max_bytes:
        return {"aborted": True, "planned_bytes": planned, "removed_bytes": 0, "count": 0}
    removed = 0
    count = 0
    for path in candidates:
        value = safe_remove(path, root, dry_run=dry_run)
        if value:
            removed += value
            count += 1
    return {"aborted": False, "planned_bytes": planned, "removed_bytes": removed, "count": count}


def _run(argv: list[str]) -> dict:
    proc = subprocess.run(argv, text=True, capture_output=True, timeout=300, check=False)
    return {
        "argv": argv[:2],
        "returncode": proc.returncode,
        "output": (proc.stdout or proc.stderr).strip()[-500:],
    }


def _disk() -> dict:
    usage = shutil.disk_usage("/")
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "used_percent": round(usage.used * 100 / usage.total, 2),
    }


def run(mode: str, *, dry_run: bool, max_bytes: int = MAX_DELETE_BYTES) -> dict:
    now = time.time()
    before = _disk()
    tmp_root = Path("/tmp")
    candidates = collect_tmp_candidates(tmp_root, now=now, older_than_days=7)
    for spool in tmp_root.glob("hermes_voice*"):
        if spool.is_dir() and not spool.is_symlink():
            candidates.extend(collect_old_files(spool, now=now, older_than_days=2))
    tmp_result = remove_candidates(candidates, tmp_root, dry_run=dry_run, max_bytes=max_bytes)

    cache_results = []
    remaining = max(0, max_bytes - int(tmp_result["planned_bytes"]))
    cache_roots: list[Path] = []
    for user in PROFILE_USERS:
        home = Path("/home") / user
        cache_roots.extend([
            home / ".hermes/cache/terminal-output",
            home / ".hermes/cache/web",
            home / ".hermes/cache/images",
        ])
    if mode == "weekly":
        cache_roots.append(Path("/home/operator/.local/share/Trash/files"))
    for root in cache_roots:
        days = 30
        paths = collect_old_files(root, now=now, older_than_days=days)
        result = remove_candidates(paths, root, dry_run=dry_run, max_bytes=remaining)
        result["root"] = str(root)
        cache_results.append(result)
        remaining = max(0, remaining - int(result["planned_bytes"]))

    commands: list[dict] = []
    if mode == "weekly" and not dry_run:
        commands.extend([
            _run(["journalctl", "--vacuum-time=14d", "--vacuum-size=500M"]),
            _run(["apt-get", "clean"]),
            _run(["docker", "builder", "prune", "-f", "--filter", "until=168h"]),
        ])
        for user in PROFILE_USERS:
            commands.append(_run(["sudo", "-n", "-u", user, "uv", "cache", "prune"]))

    after = _disk()
    report = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "mode": mode,
        "dry_run": dry_run,
        "before": before,
        "after": after,
        "freed_bytes": max(0, after["free"] - before["free"]),
        "tmp": tmp_result,
        "caches": cache_results,
        "commands": commands,
        "alert": after["used_percent"] >= 85,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("daily", "weekly"), required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-delete-gb", type=float, default=20.0)
    args = parser.parse_args()

    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", "-n", sys.executable, str(Path(__file__).resolve()), *sys.argv[1:]])

    lock_path = Path("/run/lock/agk-disk-maintenance.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "skipped", "reason": "already_running"}))
            return 0
        report = run(args.mode, dry_run=args.dry_run, max_bytes=int(args.max_delete_gb * 1024**3))
        log_path = Path("/home/operator/.hermes/logs/disk-maintenance.jsonl")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(report, sort_keys=True) + "\n")
        operator_account = pwd.getpwnam("operator")
        os.chown(log_path, operator_account.pw_uid, operator_account.pw_gid)
        os.chmod(log_path, 0o600)
        print(json.dumps(report, sort_keys=True))
        return 2 if any(item.get("aborted") for item in [report["tmp"], *report["caches"]]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
