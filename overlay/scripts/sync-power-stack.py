#!/usr/bin/env python3
"""Install AGK's pinned capability stack into one isolated Hermes profile."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


def run(command: list[str], *, env: dict[str, str], dry_run: bool = False) -> None:
    if dry_run:
        print("+ " + " ".join(command))
        return
    subprocess.run(command, check=True, env=env)


def ensure_checkout(cache: Path, repository: str, commit: str, *, env: dict[str, str], dry_run: bool) -> Path:
    name = repository.rsplit("/", 1)[1]
    target = cache / name
    if dry_run:
        print(f"+ checkout https://github.com/{repository}.git at {commit} -> {target}")
        return target
    if not (target / ".git").is_dir():
        shutil.rmtree(target, ignore_errors=True)
        run(["git", "clone", "--filter=blob:none", "--no-checkout", f"https://github.com/{repository}.git", str(target)], env=env)
    run(["git", "-C", str(target), "fetch", "--depth", "1", "origin", commit], env=env)
    run(["git", "-C", str(target), "checkout", "--detach", "--force", commit], env=env)
    actual = subprocess.check_output(["git", "-C", str(target), "rev-parse", "HEAD"], text=True, env=env).strip()
    if actual != commit:
        raise RuntimeError(f"commit mismatch for {repository}: {actual}")
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--builder", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest_path = Path(args.manifest).resolve()
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise SystemExit("unsupported power stack manifest")
    extensions = manifest.get("extensions")
    if not isinstance(extensions, dict):
        raise SystemExit("power stack extensions are missing")

    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").resolve()
    home = Path.home().resolve()
    if hermes_home == Path("/") or home not in (hermes_home, *hermes_home.parents):
        raise SystemExit("HERMES_HOME must stay inside the current Linux home")
    cache = hermes_home / "cache" / "agk-power-stack"
    if not args.dry_run:
        cache.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["HOME"] = str(home)
    env["HERMES_HOME"] = str(hermes_home)
    env.setdefault("SUPERPOWERS_DISABLE_TELEMETRY", "1")

    sources: dict[str, Path] = {}
    for name, spec in extensions.items():
        if not isinstance(spec, dict):
            raise SystemExit(f"invalid extension spec: {name}")
        repository = str(spec.get("repository") or "")
        commit = str(spec.get("commit") or "")
        if repository.count("/") != 1 or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
            raise SystemExit(f"unsafe extension pin: {name}")
        sources[name] = ensure_checkout(cache, repository, commit, env=env, dry_run=args.dry_run)

    install_root = manifest_path.parent.parent
    agk_plugin_source = install_root / "hermes" / "plugins" / "agk_power_stack"
    agk_plugin_target = hermes_home / "plugins" / "agk_power_stack"
    if args.dry_run:
        print(f"+ project {agk_plugin_source} -> {agk_plugin_target}")
    else:
        staging = agk_plugin_target.with_name("agk_power_stack.new")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(agk_plugin_source, staging)
        shutil.rmtree(agk_plugin_target, ignore_errors=True)
        staging.replace(agk_plugin_target)
    run(["hermes", "plugins", "doctor", "--ci", str(agk_plugin_target)], env=env, dry_run=args.dry_run)
    run(["hermes", "plugins", "enable", "--no-allow-tool-override", "agk-power-stack"], env=env, dry_run=args.dry_run)

    # Hermes' community scanner examines the entire upstream repository and
    # blocks this source on documentation/test fixtures that resemble dangerous
    # commands. Project only the audited 104-line Hermes adapter and its stock
    # skills into the profile instead of disabling the scanner globally.
    source = sources["superpowers"]
    plugin_target = hermes_home / "plugins" / "superpowers"
    if args.dry_run:
        print(f"+ project {source / '.hermes-plugin'} and {source / 'skills'} -> {plugin_target}")
    else:
        staging = plugin_target.with_name("superpowers.new")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(source / ".hermes-plugin", staging)
        shutil.copytree(source / "skills", staging / "skills")
        shutil.rmtree(plugin_target, ignore_errors=True)
        staging.replace(plugin_target)
    run(["hermes", "plugins", "doctor", "--ci", str(plugin_target)], env=env, dry_run=args.dry_run)
    run(["hermes", "plugins", "enable", "--no-allow-tool-override", "superpowers"], env=env, dry_run=args.dry_run)

    caveman = sources["caveman"]
    run([
        "node", str(caveman / "bin" / "install.js"), "--only", "hermes",
        "--minimal", "--force", "--non-interactive", "--no-mcp-shrink",
    ], env=env, dry_run=args.dry_run)

    agency_target = hermes_home / "skills" / "agency-agents"
    run([sys.executable, str(Path(args.builder).resolve()), str(sources["agency-agents"]), str(agency_target)], env=env, dry_run=args.dry_run)

    if not args.dry_run:
        state = {
            "schema": "agk.power-stack.state.v1",
            "extensions": {name: {"repository": spec["repository"], "commit": spec["commit"]} for name, spec in extensions.items()},
            "voice": manifest.get("voice") or {},
        }
        (cache / "state.json").write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"AGK power stack synchronized in {hermes_home}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
