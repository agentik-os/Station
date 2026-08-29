import importlib.util
import os
import time
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "overlay/scripts/agk_disk_maintenance.py"
spec = importlib.util.spec_from_file_location("agk_disk_maintenance", SCRIPT)
assert spec is not None
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_collect_candidates_only_accepts_allowlisted_prefixes_and_old_entries(tmp_path):
    now = time.time()
    old_allowed = tmp_path / "hermes_voice-old"
    old_denied = tmp_path / "customer-data"
    fresh_allowed = tmp_path / "pytest-of-operator"
    for path in (old_allowed, old_denied, fresh_allowed):
        path.mkdir()
        (path / "payload").write_bytes(b"x" * 10)
    old = now - 10 * 86400
    os.utime(old_allowed, (old, old))
    os.utime(old_denied, (old, old))

    candidates = module.collect_tmp_candidates(tmp_path, now=now, older_than_days=7)

    assert [p.name for p in candidates] == ["hermes_voice-old"]


def test_symlink_candidate_is_never_removed(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "hermes_voice-link"
    link.symlink_to(target, target_is_directory=True)

    assert module.safe_size(link, tmp_path) == 0
    assert module.safe_remove(link, tmp_path, dry_run=False) == 0
    assert target.exists()
    assert link.is_symlink()


def test_deletion_cap_aborts_without_partial_removal(tmp_path):
    first = tmp_path / "hermes_voice-a"
    second = tmp_path / "pytest-of-a"
    for path in (first, second):
        path.mkdir()
        (path / "payload").write_bytes(b"x" * 8)

    result = module.remove_candidates([first, second], tmp_path, dry_run=False, max_bytes=10)

    assert result["aborted"] is True
    assert first.exists() and second.exists()
