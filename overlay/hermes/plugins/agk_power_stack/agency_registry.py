"""Profile-local Agency Agents roster search and brief retrieval."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,119}$")
_TOKEN = re.compile(r"[a-z0-9]+")

AGENCY_TOOL_SCHEMA = {
    "name": "agency_specialist",
    "description": "Search and load a profile-local Agency Agents specialist brief.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "search", "get"]},
            "query": {"type": "string"},
            "id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["action"],
    },
}


def _root() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").resolve()
    return home / "skills" / "agency-agents"


def _index() -> list[dict[str, str]]:
    path = _root() / "index.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    agents = document.get("agents") if isinstance(document, dict) else []
    if not isinstance(agents, list):
        return []
    return [row for row in agents if isinstance(row, dict) and _ID.fullmatch(str(row.get("id") or ""))]


def agency_available() -> bool:
    return bool(_index())


def _public(row: dict[str, Any]) -> dict[str, str]:
    return {key: str(row.get(key) or "") for key in ("id", "name", "description", "category")}


def _search(query: str, limit: int) -> list[dict[str, str]]:
    terms = set(_TOKEN.findall(query.lower()))
    ranked = []
    for row in _index():
        searchable = " ".join(str(row.get(k) or "") for k in ("id", "name", "description", "category")).lower()
        tokens = set(_TOKEN.findall(searchable))
        score = len(terms & tokens) * 10 + sum(2 for term in terms if term in searchable)
        if not terms or score:
            ranked.append((score, str(row.get("id")), row))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [_public(row) for _, _, row in ranked[:limit]]


def handle_agency(args: dict[str, Any]) -> dict[str, Any]:
    action = str(args.get("action") or "").strip().lower()
    try:
        limit = max(1, min(20, int(args.get("limit", 8))))
    except (TypeError, ValueError):
        limit = 8
    if action == "list":
        return {"success": True, "specialists": [_public(row) for row in _index()[:limit]]}
    if action == "search":
        return {"success": True, "specialists": _search(str(args.get("query") or ""), limit)}
    if action != "get":
        return {"success": False, "error": "action must be list, search, or get"}
    agent_id = str(args.get("id") or "")
    if not _ID.fullmatch(agent_id):
        return {"success": False, "error": "invalid specialist id"}
    row = next((item for item in _index() if item.get("id") == agent_id), None)
    if row is None:
        return {"success": False, "error": "specialist not found"}
    reference = str(row.get("reference") or "")
    path = (_root() / reference).resolve()
    references = (_root() / "references").resolve()
    if path.parent != references or path.name != f"{agent_id}.md":
        return {"success": False, "error": "unsafe specialist reference"}
    try:
        brief = path.read_text(encoding="utf-8")
    except OSError:
        return {"success": False, "error": "specialist brief is unavailable"}
    return {"success": True, "specialist": _public(row), "brief": brief}
