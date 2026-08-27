#!/usr/bin/env python3
"""Install AGK's canonical core Operative System packages transactionally."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
LIST_FIELDS = (
    "dependencies", "capabilities", "skills", "workflows", "agents", "tools",
    "commands", "knowledge", "evals",
)


def _manifest(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(f"invalid manifest: {path}") from error
    if not isinstance(raw, dict):
        raise ValueError(f"invalid manifest mapping: {path}")
    package_id = raw.get("id")
    version = raw.get("version")
    if not isinstance(package_id, str) or not _ID.fullmatch(package_id):
        raise ValueError(f"invalid package identity: {path}")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError(f"invalid package version: {path}")
    for field in ("name", "description"):
        if not isinstance(raw.get(field), str) or not raw[field].strip():
            raise ValueError(f"missing {field}: {path}")
    scopes = raw.get("scope")
    if not isinstance(scopes, list) or not scopes or any(not isinstance(value, str) for value in scopes):
        raise ValueError(f"invalid package scope: {path}")
    for field in LIST_FIELDS:
        values = raw.get(field)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise ValueError(f"invalid package list {field}: {path}")
    return raw


def _files(root: Path) -> list[Path]:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"symlink rejected in package: {path}")
        if path.is_file():
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError(f"oversized package file: {path}")
            files.append(path)
    if not files or len(files) > 500:
        raise ValueError(f"invalid package file count: {root}")
    return files


def _checksum(root: Path) -> str:
    digest = hashlib.sha256()
    for path in _files(root):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _entry(manifest: dict[str, Any], package_dir: Path) -> dict[str, Any]:
    files = _files(package_dir)
    entry = {key: manifest[key] for key in (
        "schema_version", "id", "name", "version", "description", "scope",
        *LIST_FIELDS,
    ) if key in manifest}
    entry.update({
        "checksum": _checksum(package_dir),
        "file_count": len(files),
        "uncompressed_bytes": sum(path.stat().st_size for path in files),
        "status": manifest.get("status", "active"),
        "license": manifest.get("license", "AGK-internal"),
    })
    return entry


def install_packages(source: Path, registry: Path) -> list[str]:
    source = source.resolve()
    registry = registry.resolve()
    if not source.is_dir() or source.is_symlink():
        raise ValueError(f"unsafe package source: {source}")
    packages_root = registry / "packages"
    state_root = registry / "state"
    packages_root.mkdir(parents=True, exist_ok=True)
    state_root.mkdir(parents=True, exist_ok=True)
    index_path = state_root / "index.json"
    try:
        document = json.loads(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        document = {"schema_version": 1, "packages": []}
    except (OSError, ValueError) as error:
        raise ValueError("registry index is invalid") from error
    existing = document.get("packages") if isinstance(document, dict) else None
    if not isinstance(existing, list):
        raise ValueError("registry packages list is invalid")
    by_identity = {
        (item.get("id"), item.get("version")): item
        for item in existing if isinstance(item, dict)
    }
    installed = []
    for package_source in sorted(path for path in source.iterdir() if path.is_dir()):
        manifest = _manifest(package_source / "manifest.yaml")
        if manifest["id"] != package_source.name:
            raise ValueError(f"package identity does not match directory: {package_source}")
        version = manifest["version"]
        target = packages_root / manifest["id"] / version
        source_checksum = _checksum(package_source)
        if target.exists():
            if not target.is_dir() or target.is_symlink() or _checksum(target) != source_checksum:
                raise ValueError(f"installed package differs from immutable source: {target}")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=target.parent))
            try:
                shutil.copytree(package_source, staging, dirs_exist_ok=True)
                os.replace(staging, target)
            finally:
                shutil.rmtree(staging, ignore_errors=True)
        by_identity[(manifest["id"], version)] = _entry(manifest, target)
        installed.append(f"{manifest['id']}@{version}")
    final = sorted(by_identity.values(), key=lambda item: (str(item.get("id")), str(item.get("version"))))
    payload = {"schema_version": 1, "packages": final}
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=state_root, prefix=".index.", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, index_path)
    finally:
        temporary.unlink(missing_ok=True)
    return installed


CORE_REFERENCES = (
    "research-os@0.1.0",
    "strategy-os@0.1.0",
    "builder-os@0.1.0",
    "evaluation-os@0.1.0",
)


def _write_assignments(path: Path, rows: list[dict[str, Any]], owner: tuple[int, int] | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".assignments.", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            yaml.safe_dump({"schema_version": 1, "assignments": rows}, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        if owner is not None:
            os.chown(temporary, owner[0], owner[1])
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def reconcile_fleet_assignments(homes_root: Path, operator_path: Path) -> None:
    for organisation in ("operator", "agentik", "mission", "private"):
        home = homes_root / organisation
        path = operator_path if organisation == "operator" else home / ".agentik" / "os-assignments.yaml"
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            document = {}
        rows = document.get("assignments") if isinstance(document, dict) else []
        records = [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        for reference in CORE_REFERENCES:
            record = {"os": reference, "scope": "environment", "target": organisation}
            if record not in records:
                records.append(record)
        owner = None
        try:
            stat = home.stat()
            owner = (stat.st_uid, stat.st_gid) if organisation != "operator" else None
        except OSError:
            pass
        _write_assignments(path, records, owner)


def _chown_tree(root: Path, owner: tuple[int, int] | None) -> None:
    if owner is None:
        return
    for path in [root, *root.rglob("*")]:
        os.chown(path, owner[0], owner[1], follow_symlinks=False)


def project_core_skills(source: Path, homes_root: Path) -> None:
    skills: dict[str, Path] = {}
    for package in sorted(path for path in source.iterdir() if path.is_dir()):
        skills_root = package / "skills"
        if not skills_root.is_dir() or skills_root.is_symlink():
            continue
        for skill in sorted(path for path in skills_root.iterdir() if path.is_dir()):
            skill_id = skill.name
            if not _ID.fullmatch(skill_id) or not (skill / "SKILL.md").is_file():
                raise ValueError(f"invalid core OS skill: {skill}")
            if skill_id in skills:
                raise ValueError(f"duplicate core OS skill: {skill_id}")
            _files(skill)
            skills[skill_id] = skill
    for organisation in ("operator", "agentik", "mission", "private"):
        home = homes_root / organisation
        hermes = home / ".hermes"
        targets = [hermes]
        profiles = hermes / "profiles"
        if profiles.is_dir() and not profiles.is_symlink():
            targets.extend(path for path in sorted(profiles.iterdir()) if path.is_dir() and not path.is_symlink())
        try:
            stat = home.stat()
            owner: tuple[int, int] | None = (stat.st_uid, stat.st_gid)
        except OSError:
            owner = None
        for target_home in targets:
            category = target_home / "skills" / "core-os"
            category.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".core-os.", dir=category.parent))
            try:
                for skill_id, source_skill in skills.items():
                    shutil.copytree(source_skill, staging / skill_id)
                _chown_tree(staging, owner)
                shutil.rmtree(category, ignore_errors=True)
                os.replace(staging, category)
            finally:
                shutil.rmtree(staging, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="/usr/local/lib/agk-terminal/os-packages")
    parser.add_argument("--registry", default="/opt/agentik/os-registry")
    parser.add_argument("--assign-fleet", action="store_true")
    args = parser.parse_args()
    installed = install_packages(Path(args.source), Path(args.registry))
    if args.assign_fleet:
        reconcile_fleet_assignments(
            Path("/home"), Path("/etc/agentik/operator-os/assignments.yaml")
        )
        project_core_skills(Path(args.source), Path("/home"))
    print("Installed core OS packages: " + ", ".join(installed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
