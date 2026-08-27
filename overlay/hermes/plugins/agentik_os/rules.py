"""Global AGK rules injected into every Hermes and OpenRouter conversation."""

from __future__ import annotations

import os
from pathlib import Path

import yaml


def _read_rules(path: Path) -> list[dict]:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, TypeError, ValueError, yaml.YAMLError):
        return []
    rules = document.get("rules") or []
    return [rule for rule in rules if isinstance(rule, dict)]


def _merge_rules(base: list[dict], overrides: list[dict]) -> list[dict]:
    merged = {str(rule.get("id") or f"anonymous-{index}"): rule for index, rule in enumerate(base)}
    for index, rule in enumerate(overrides):
        merged[str(rule.get("id") or f"profile-anonymous-{index}")] = rule
    return list(merged.values())


def active_rules() -> list[dict]:
    configured = os.environ.get("AGK_RULES_CONFIG")
    if configured:
        rules = _read_rules(Path(configured).expanduser())
    else:
        root = Path(os.environ.get("AGK_TERMINAL_ROOT", "/usr/local/lib/agk-terminal"))
        system = Path("/etc/agk-terminal/rules.yaml")
        if not system.is_file():
            system = root / "config" / "rules.yaml"
        user = Path.home() / ".agentik" / "rules.yaml"
        rules = _merge_rules(_read_rules(system), _read_rules(user))
    return [
        rule
        for rule in rules
        if isinstance(rule, dict)
        and rule.get("enabled", True)
        and str(rule.get("content") or "").strip()
        and (
            "*" in (rule.get("providers") or ["*"])
            or "hermes" in (rule.get("providers") or [])
            or "openrouter" in (rule.get("providers") or [])
        )
    ]


def rules_prompt(_session_info: dict | None = None) -> str:
    rules = active_rules()
    if not rules:
        return ""
    rendered = ["AGK operator rules (apply to every provider session):"]
    for rule in rules:
        title = str(rule.get("title") or rule.get("id") or "Rule").strip()
        content = str(rule.get("content") or "").strip()
        rendered.append(f"- {title}: {content}")
    return "\n".join(rendered)
