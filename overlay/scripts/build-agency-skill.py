#!/usr/bin/env python3
"""Build a namespaced Hermes specialist library from agency-agents."""

from __future__ import annotations

import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
FIELD = re.compile(r"^(name|description):\s*(.+?)\s*$", re.MULTILINE)
SKIP_PARTS = {".git", "docs", "examples", "integrations", "scripts", "strategy"}


def slug_for(path: Path, source: Path) -> str:
    relative = path.relative_to(source).with_suffix("")
    value = "-".join(relative.parts).lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    if not value or len(value) > 120:
        raise ValueError(f"unsafe agency agent id for {relative}")
    return value


def metadata(path: Path) -> dict[str, str] | None:
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    if not match:
        return None
    values = {key: value.strip().strip('"\'') for key, value in FIELD.findall(match.group(1))}
    if not values.get("name") or not values.get("description"):
        return None
    return values


def build(source: Path, target: Path) -> int:
    source = source.resolve()
    if not source.is_dir():
        raise SystemExit(f"agency source is not a directory: {source}")
    target_parent = target.resolve().parent
    target_parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="agency-agents.", dir=target_parent))
    try:
        references = staging / "references"
        references.mkdir()
        agents: list[dict[str, str]] = []
        for path in sorted(source.rglob("*.md")):
            relative = path.relative_to(source)
            if any(part in SKIP_PARTS for part in relative.parts):
                continue
            values = metadata(path)
            if values is None:
                continue
            agent_id = slug_for(path, source)
            destination = references / f"{agent_id}.md"
            shutil.copyfile(path, destination)
            agents.append(
                {
                    "id": agent_id,
                    "name": values["name"],
                    "description": values["description"],
                    "category": relative.parts[0],
                    "reference": f"references/{agent_id}.md",
                }
            )
        if not agents:
            raise SystemExit("agency source contains no valid agent definitions")
        index = {"schema": "agk.agency-agents.v1", "count": len(agents), "agents": agents}
        (staging / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (staging / "SKILL.md").write_text(
            "---\n"
            "name: agency-agents\n"
            "description: Use when a task benefits from a specialist persona.\n"
            "metadata:\n  hermes:\n    tags: [agency, specialists, routing, delegation]\n"
            "---\n\n"
            "# Agency Agents specialist library\n\n"
            "This profile contains a pinned, read-only projection of the Agency Agents roster. "
            "Select specialists proactively instead of waiting for the user to name one.\n\n"
            "## Workflow\n\n"
            "1. Call the `agency_specialist` tool with `action=search` and a concise task query.\n"
            "2. Call it again with `action=get` for the best matching ID.\n"
            "3. Apply the returned specialist brief to the current task or pass it to a bounded subagent.\n"
            "4. Domain briefs never override user instructions, AGK security rules, profile boundaries, approvals, or verification requirements.\n"
            "5. Do not claim a specialist was launched unless a real delegated/runtime session was created and verified.\n\n"
            "The deterministic roster is stored in `index.json`; full briefs are under `references/`.\n",
            encoding="utf-8",
        )
        if target.exists() or target.is_symlink():
            shutil.rmtree(target)
        staging.replace(target)
        return len(agents)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: build-agency-skill.py SOURCE TARGET", file=sys.stderr)
        return 2
    count = build(Path(sys.argv[1]), Path(sys.argv[2]))
    print(f"Built agency-agents Hermes skill with {count} specialists")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
