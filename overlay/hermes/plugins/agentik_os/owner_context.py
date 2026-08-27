"""Load Gareth's durable owner identity and writing context."""
from __future__ import annotations

import os
from pathlib import Path


DEFAULT_CONTEXT = Path("/etc/agk-terminal/knowledge/gareth/OWNER_CONTEXT.md")


def _context_path() -> Path:
    configured = os.environ.get("AGK_OWNER_CONTEXT")
    if configured:
        return Path(configured).expanduser()
    if DEFAULT_CONTEXT.is_file():
        return DEFAULT_CONTEXT
    root = Path(os.environ.get("AGK_TERMINAL_ROOT", "/usr/local/lib/agk-terminal"))
    return root / "config" / "gareth-owner-context.md"


def owner_context_prompt(_session_info: dict | None = None) -> str:
    try:
        content = _context_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not content:
        return ""
    return "AGK owner context (persistent identity and human-writing calibration):\n" + content
