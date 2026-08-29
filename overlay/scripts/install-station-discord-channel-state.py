#!/usr/bin/env python3
"""Install, audit, or rollback AGK Discord channel-state projectors."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pwd
import shutil
import stat
import subprocess
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECTOR_SOURCE = HERE / "station_discord_channel_state.py"
MANIFEST_SOURCE = HERE.parent / "config" / "discord-channel-state.json"
INSTALL_ROOT = Path("/usr/local/lib/agk-terminal/scripts")
PROJECTOR_INSTALLED = INSTALL_ROOT / "station_discord_channel_state.py"
BACKUP_ROOT = Path("/var/lib/station/backups/discord-channel-state")
UNIT_ROOT = Path("/etc/systemd/user")
INSTALLER_UID = os.geteuid()
INSTALLER_GID = os.getegid()

SPEC = importlib.util.spec_from_file_location("station_discord_channel_state", PROJECTOR_SOURCE)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


def open_directory_no_symlinks(path: Path) -> int:
    """Open an absolute directory one component at a time without following symlinks."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("secure directory path must be absolute")
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def ensure_trusted_directory(
    path: Path,
    *,
    create: bool,
    trusted_ancestor: Path = Path("/"),
) -> None:
    """Require a non-symlink directory chain owned by the installer and not writable by peers."""
    path = Path(path)
    trusted_ancestor = Path(trusted_ancestor)
    try:
        relative = path.relative_to(trusted_ancestor)
    except ValueError as exc:
        raise ValueError("trusted directory must remain below its anchor") from exc
    descriptor = open_directory_no_symlinks(trusted_ancestor)
    try:
        for index, component in enumerate(relative.parts):
            try:
                next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o700 if index == len(relative.parts) - 1 else 0o755, dir_fd=descriptor)
                next_descriptor = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            metadata = os.fstat(descriptor)
            if metadata.st_uid != INSTALLER_UID or metadata.st_mode & 0o022:
                raise PermissionError(f"untrusted directory ownership or mode: {path}")
    finally:
        os.close(descriptor)


def secure_mkdir_child(parent: Path, name: str, mode: int = 0o700) -> Path:
    descriptor = open_directory_no_symlinks(parent)
    try:
        os.mkdir(name, mode, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    return Path(parent) / name


def secure_unlink(path: Path, *, missing_ok: bool = True) -> None:
    descriptor = open_directory_no_symlinks(Path(path).parent)
    try:
        try:
            os.unlink(Path(path).name, dir_fd=descriptor)
        except FileNotFoundError:
            if not missing_ok:
                raise
    finally:
        os.close(descriptor)


def secure_regular_stat(path: Path) -> os.stat_result | None:
    descriptor = open_directory_no_symlinks(Path(path).parent)
    try:
        try:
            metadata = os.stat(Path(path).name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"path must be a regular file: {path}")
        return metadata
    finally:
        os.close(descriptor)


def atomic_install_file(
    source: Path,
    destination: Path,
    *,
    staging_dir: Path,
    mode: int,
    uid: int,
    gid: int,
    preserve_metadata: bool = False,
) -> None:
    """Install with pinned source, staging, and destination directory descriptors."""
    staging_descriptor = -1
    destination_descriptor = -1
    source_parent_descriptor = -1
    temporary_name = f"install-{uuid.uuid4().hex}"
    temporary_descriptor = -1
    source_descriptor = -1
    try:
        staging_descriptor = open_directory_no_symlinks(Path(staging_dir))
        destination_descriptor = open_directory_no_symlinks(Path(destination).parent)
        source_parent_descriptor = open_directory_no_symlinks(Path(source).parent)
        staging_metadata = os.fstat(staging_descriptor)
        if staging_metadata.st_uid != INSTALLER_UID or staging_metadata.st_mode & 0o022:
            raise PermissionError("staging directory is not installer-controlled")
        destination_parent_metadata = os.fstat(destination_descriptor)
        destination_parent_identity = (
            destination_parent_metadata.st_dev,
            destination_parent_metadata.st_ino,
        )
        if destination_parent_metadata.st_uid != INSTALLER_UID or destination_parent_metadata.st_mode & 0o022:
            raise PermissionError("destination directory is not installer-controlled")
        source_descriptor = os.open(
            Path(source).name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            dir_fd=source_parent_descriptor,
        )
        source_metadata = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_metadata.st_mode):
            raise RuntimeError("install source must be a regular file")
        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
            dir_fd=staging_descriptor,
        )
        with os.fdopen(os.dup(temporary_descriptor), "wb") as output, os.fdopen(os.dup(source_descriptor), "rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.fchown(temporary_descriptor, uid, gid)
        os.fchmod(temporary_descriptor, mode)
        if preserve_metadata:
            for attribute in os.listxattr(source_descriptor):
                os.setxattr(temporary_descriptor, attribute, os.getxattr(source_descriptor, attribute))
            os.utime(
                temporary_name,
                ns=(source_metadata.st_atime_ns, source_metadata.st_mtime_ns),
                dir_fd=staging_descriptor,
                follow_symlinks=False,
            )
        os.replace(
            temporary_name,
            Path(destination).name,
            src_dir_fd=staging_descriptor,
            dst_dir_fd=destination_descriptor,
        )
        visible_descriptor = open_directory_no_symlinks(Path(destination).parent)
        try:
            visible_metadata = os.fstat(visible_descriptor)
            if (visible_metadata.st_dev, visible_metadata.st_ino) != destination_parent_identity:
                raise RuntimeError("destination parent changed during atomic install")
        finally:
            os.close(visible_descriptor)
    finally:
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if staging_descriptor >= 0:
            try:
                os.unlink(temporary_name, dir_fd=staging_descriptor)
            except FileNotFoundError:
                pass
        for descriptor in (source_parent_descriptor, destination_descriptor, staging_descriptor):
            if descriptor >= 0:
                os.close(descriptor)


def user_systemctl(user: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    account = pwd.getpwnam(user)
    env = [
        f"HOME={account.pw_dir}",
        f"XDG_RUNTIME_DIR=/run/user/{account.pw_uid}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{account.pw_uid}/bus",
        "PATH=/usr/local/bin:/usr/bin:/bin",
    ]
    return subprocess.run(
        [
            "/usr/bin/setpriv",
            f"--reuid={account.pw_uid}",
            f"--regid={account.pw_gid}",
            "--clear-groups",
            "/usr/bin/env",
            *env,
            "/usr/bin/systemctl",
            "--user",
            *args,
        ],
        text=True,
        capture_output=True,
        timeout=60,
        check=check,
    )


def unit_path(target: dict) -> Path:
    return UNIT_ROOT / f"station-discord-channel-state-{target['key']}.service"


def legacy_unit_path(target: dict) -> Path:
    return Path(f"/home/{target['user']}/.config/systemd/user/station-discord-channel-state-{target['key']}.service")


def user_remove_legacy_unit(target: dict) -> subprocess.CompletedProcess[str]:
    """Remove a legacy per-home unit with only the target user's authority."""
    account = pwd.getpwnam(target["user"])
    path = legacy_unit_path(target)
    expected_parent = Path(account.pw_dir) / ".config/systemd/user"
    if path.parent != expected_parent:
        raise ValueError("legacy unit escaped the target user boundary")
    return subprocess.run(
        [
            "/usr/bin/setpriv",
            f"--reuid={account.pw_uid}",
            f"--regid={account.pw_gid}",
            "--clear-groups",
            "/usr/bin/rm",
            "-f",
            "--",
            str(path),
        ],
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )


def install(manifest: Path) -> dict:
    if os.geteuid() != 0:
        raise PermissionError("root is required")
    targets = MODULE.load_targets(manifest)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    ensure_trusted_directory(BACKUP_ROOT, create=True)
    ensure_trusted_directory(INSTALL_ROOT, create=True)
    ensure_trusted_directory(UNIT_ROOT, create=True)
    backup = secure_mkdir_child(BACKUP_ROOT, timestamp)
    staging = secure_mkdir_child(backup, "staging")
    installed_units: list[str] = []
    prior_units: dict[str, dict] = {}
    users: set[str] = set()
    projector_stat = secure_regular_stat(PROJECTOR_INSTALLED)
    projector_existed = projector_stat is not None
    projector_prior = None
    if projector_existed:
        assert projector_stat is not None
        projector_prior = {
            "mode": stat.S_IMODE(projector_stat.st_mode),
            "uid": projector_stat.st_uid,
            "gid": projector_stat.st_gid,
        }
        atomic_install_file(
            PROJECTOR_INSTALLED,
            backup / PROJECTOR_INSTALLED.name,
            staging_dir=staging,
            mode=projector_prior["mode"],
            uid=INSTALLER_UID,
            gid=INSTALLER_GID,
            preserve_metadata=True,
        )
    staged_projector = staging / PROJECTOR_INSTALLED.name
    if secure_regular_stat(PROJECTOR_SOURCE) is None:
        raise RuntimeError("projector source is unavailable")
    atomic_install_file(
        PROJECTOR_SOURCE,
        staged_projector,
        staging_dir=staging,
        mode=0o755,
        uid=INSTALLER_UID,
        gid=INSTALLER_GID,
    )

    for target in targets:
        pwd.getpwnam(target["user"])
        path = unit_path(target)
        existing_stat = secure_regular_stat(path)
        existed = existing_stat is not None
        metadata = None
        if existed:
            assert existing_stat is not None
            metadata = {
                "mode": stat.S_IMODE(existing_stat.st_mode),
                "uid": existing_stat.st_uid,
                "gid": existing_stat.st_gid,
            }
            atomic_install_file(
                path,
                backup / path.name,
                staging_dir=staging,
                mode=metadata["mode"],
                uid=INSTALLER_UID,
                gid=INSTALLER_GID,
                preserve_metadata=True,
            )
        prior_units[str(path)] = {
            "existed": existed,
            "enabled": user_systemctl(target["user"], "is-enabled", path.name, check=False).returncode == 0,
            "active": user_systemctl(target["user"], "is-active", path.name, check=False).returncode == 0,
            "user": target["user"],
            "metadata": metadata,
        }
        staged_unit = staging / path.name
        staged_unit.write_text(MODULE.render_unit(target, script_path=str(PROJECTOR_INSTALLED)), encoding="utf-8")
        users.add(target["user"])

    mutated_units: list[dict] = []
    projector_mutated = False
    try:
        # Mark rollback responsibility before each atomic call so even a
        # post-rename cleanup failure cannot escape the transaction.
        projector_mutated = True
        atomic_install_file(
            staged_projector,
            PROJECTOR_INSTALLED,
            staging_dir=staging,
            mode=0o755,
            uid=INSTALLER_UID,
            gid=INSTALLER_GID,
        )
        for target in targets:
            path = unit_path(target)
            mutated_units.append(target)
            atomic_install_file(
                staging / path.name,
                path,
                staging_dir=staging,
                mode=0o644,
                uid=INSTALLER_UID,
                gid=INSTALLER_GID,
            )
            installed_units.append(str(path))
        for user in sorted(users):
            user_systemctl(user, "daemon-reload")
        for target in targets:
            name = unit_path(target).name
            user_systemctl(target["user"], "enable", name)
            user_systemctl(target["user"], "restart", name)
    except Exception:
        for target in reversed(mutated_units):
            path = unit_path(target)
            prior = prior_units[str(path)]
            user_systemctl(target["user"], "disable", "--now", path.name, check=False)
            saved = backup / path.name
            if prior["existed"] and saved.exists():
                metadata = prior["metadata"]
                atomic_install_file(
                    saved,
                    path,
                    staging_dir=staging,
                    mode=metadata["mode"],
                    uid=metadata["uid"],
                    gid=metadata["gid"],
                    preserve_metadata=True,
                )
            else:
                secure_unlink(path)
        if projector_mutated:
            if projector_existed and (backup / PROJECTOR_INSTALLED.name).exists():
                assert projector_prior is not None
                atomic_install_file(
                    backup / PROJECTOR_INSTALLED.name,
                    PROJECTOR_INSTALLED,
                    staging_dir=staging,
                    mode=projector_prior["mode"],
                    uid=projector_prior["uid"],
                    gid=projector_prior["gid"],
                    preserve_metadata=True,
                )
            else:
                secure_unlink(PROJECTOR_INSTALLED)
        for user in sorted(users):
            user_systemctl(user, "daemon-reload", check=False)
        for path_text, prior in prior_units.items():
            path = Path(path_text)
            if prior["enabled"]:
                user_systemctl(prior["user"], "enable", path.name, check=False)
            if prior["active"]:
                user_systemctl(prior["user"], "start", path.name, check=False)
        raise

    # Per-home unit directories are user-controlled. Once root-controlled
    # global user units are live, remove only the obsolete shadow copies while
    # running with each target user's own authority, then reload exactly once.
    migrated_legacy_units = []
    for target in targets:
        legacy = legacy_unit_path(target)
        user_remove_legacy_unit(target)
        migrated_legacy_units.append(str(legacy))
    for user in sorted(users):
        user_systemctl(user, "daemon-reload")
    for target in targets:
        user_systemctl(target["user"], "restart", unit_path(target).name)

    return {
        "status": "installed",
        "backup": str(backup),
        "script": str(PROJECTOR_INSTALLED),
        "units": installed_units,
        "migrated_legacy_units": migrated_legacy_units,
    }


def audit(manifest: Path) -> dict:
    targets = MODULE.load_targets(manifest)
    rows = []
    for target in targets:
        name = unit_path(target).name
        result = user_systemctl(
            target["user"],
            "show",
            name,
            "-p", "ActiveState",
            "-p", "SubState",
            "-p", "MainPID",
            "-p", "NRestarts",
            check=False,
        )
        values = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        rows.append({
            "key": target["key"],
            "user": target["user"],
            "channel_id": target["channel_id"],
            "unit": name,
            **values,
        })
    return {"status": "audited", "targets": rows}


def rollback(manifest: Path) -> dict:
    if os.geteuid() != 0:
        raise PermissionError("root is required")
    targets = MODULE.load_targets(manifest)
    users: set[str] = set()
    restored = []
    errors = []
    for target in targets:
        name = unit_path(target).name
        user_systemctl(target["user"], "disable", "--now", name, check=False)
        account = pwd.getpwnam(target["user"])
        command = [
            "/usr/bin/setpriv", f"--reuid={account.pw_uid}", f"--regid={account.pw_gid}",
            "--clear-groups", "/usr/bin/env", f"HOME={account.pw_dir}",
            f"HERMES_HOME={target['hermes_home']}", "/usr/bin/python3", str(PROJECTOR_INSTALLED),
            "--hermes-home", target["hermes_home"], "restore",
            "--channel-id", target["channel_id"], "--base-name", target["base_name"],
            "--parent-id", target["parent_id"], "--position", str(target["position"]),
        ]
        restored_target = False
        try:
            result = subprocess.run(command, text=True, capture_output=True, timeout=60, check=True)
            restored.append(json.loads(result.stdout))
            restored_target = True
        except (subprocess.SubprocessError, ValueError) as exc:
            errors.append({"key": target["key"], "error": type(exc).__name__})
        if restored_target:
            secure_unlink(unit_path(target))
        else:
            # Preserve a working, retryable projector when Discord defers rollback.
            user_systemctl(target["user"], "enable", "--now", name, check=False)
        users.add(target["user"])
    for user in sorted(users):
        user_systemctl(user, "daemon-reload")
    if not errors:
        secure_unlink(PROJECTOR_INSTALLED)
    return {
        "status": "rolled-back" if not errors else "rollback-incomplete",
        "restored": restored,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "audit", "rollback"))
    parser.add_argument("--manifest", type=Path, default=MANIFEST_SOURCE)
    args = parser.parse_args()
    if args.action == "install":
        result = install(args.manifest)
    elif args.action == "rollback":
        result = rollback(args.manifest)
    else:
        result = audit(args.manifest)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result.get("status") == "rollback-incomplete" else 0


if __name__ == "__main__":
    raise SystemExit(main())
