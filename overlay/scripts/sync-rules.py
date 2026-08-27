#!/usr/bin/env python3
"""Project AGK's managed global rule block into supported provider files."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

START = "<!-- AGK MANAGED RULES: START -->"
END = "<!-- AGK MANAGED RULES: END -->"


def load_rules(path: Path) -> list[dict]:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules = document.get("rules") or []
    return [
        rule
        for rule in rules
        if isinstance(rule, dict)
        and rule.get("enabled", True)
        and str(rule.get("content") or "").strip()
    ]


def _merge_rules(base: list[dict], overrides: list[dict]) -> list[dict]:
    merged = {str(rule.get("id") or f"anonymous-{index}"): rule for index, rule in enumerate(base)}
    for index, rule in enumerate(overrides):
        merged[str(rule.get("id") or f"profile-anonymous-{index}")] = rule
    return list(merged.values())


def load_effective_rules(
    *,
    system_path: Path | None = None,
    user_path: Path | None = None,
    power_path: Path | None = None,
) -> list[dict]:
    configured = os.environ.get("AGK_RULES_CONFIG")
    if configured:
        return load_rules(Path(configured).expanduser())
    root = Path(os.environ.get("AGK_TERMINAL_ROOT", "/usr/local/lib/agk-terminal"))
    system_path = system_path or (Path("/etc/agk-terminal/rules.yaml") if Path("/etc/agk-terminal/rules.yaml").is_file() else root / "config" / "rules.yaml")
    user_path = user_path or Path.home() / ".agentik" / "rules.yaml"
    power_path = power_path or root / "config" / "power-stack.yaml"
    base = load_rules(system_path) if system_path.is_file() else []
    power = load_rules(power_path) if power_path.is_file() else []
    overrides = load_rules(user_path) if user_path.is_file() else []
    return _merge_rules(_merge_rules(base, power), overrides)


def applies(rule: dict, provider: str) -> bool:
    providers = rule.get("providers") or ["*"]
    return "*" in providers or provider in providers


def render(rules: list[dict], provider: str) -> str:
    lines = [START, "# AGK global rules", ""]
    for rule in rules:
        if not applies(rule, provider):
            continue
        title = str(rule.get("title") or rule.get("id") or "Rule").strip()
        content = str(rule.get("content") or "").strip()
        lines.extend([f"## {title}", "", content, ""])
    lines.append(END)
    return "\n".join(lines) + "\n"


def update(path: Path, block: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"refusing symlinked provider rules file: {path}")
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    if START in current and END in current:
        before, remainder = current.split(START, 1)
        _, after = remainder.split(END, 1)
        prefix = before.rstrip()
        updated = (prefix + "\n\n" if prefix else "") + block + after.lstrip("\n")
    else:
        updated = current.rstrip() + ("\n\n" if current.strip() else "") + block
    temporary = path.with_name(f".{path.name}.agk-new")
    temporary.write_text(updated, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def main() -> int:
    rules = load_effective_rules()
    targets = {
        "claude": Path.home() / ".claude" / "CLAUDE.md",
        "codex": Path.home() / ".codex" / "AGENTS.md",
        "opencode": Path.home() / ".config" / "opencode" / "AGENTS.md",
    }
    for provider, target in targets.items():
        update(target, render(rules, provider))
        print(f"{provider}: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
