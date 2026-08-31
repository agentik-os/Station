"""Deterministic, platform-safe Discord interaction surfaces.

This module contains no gateway state and no discord.py dependency.  It turns a
validated decision contract into bounded visible content; adapter Views own the
interactive controls and authorization lifecycle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, Sequence


class SurfaceKind(str, Enum):
    SIMPLE = "simple"
    COMPLEX = "complex"
    RISK = "risk"
    APPROVAL = "approval"
    OPEN_TEXT = "open_text"
    BATCH = "batch"


def utf16_len(value: str) -> int:
    """Return Discord's UTF-16 code-unit length for *value*."""
    return len(value.encode("utf-16-le")) // 2


def _prefix_utf16(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    used = 0
    result = []
    for char in value:
        width = utf16_len(char)
        if used + width > limit:
            break
        result.append(char)
        used += width
    return "".join(result)


_PRIVATE_PATH_RE = re.compile(r"/home/[^\s`]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|password|secret|api[_-]?key)\s*=\s*([^\s,;]+)"
)


def sanitize_visible_text(value: object) -> str:
    """Remove common private-path and inline-secret shapes from visible copy."""
    text = str(value or "").strip()
    text = _PRIVATE_PATH_RE.sub("[private path]", text)
    return _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[redacted]", text)


@dataclass(frozen=True)
class DecisionChoice:
    id: str
    label: str
    consequence: str
    recommended: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        for field_name in ("id", "label", "consequence"):
            if not sanitize_visible_text(getattr(self, field_name)):
                raise ValueError(f"choice {field_name} is required")


@dataclass(frozen=True)
class DecisionRequest:
    decision_id: str
    title: str
    state: str
    target: str
    decision: str
    choices: Sequence[DecisionChoice]
    default_action: str
    kind: Optional[SurfaceKind] = None
    context: str = ""
    established: Sequence[str] = ()
    recommendation: str = ""
    risk: str = ""
    includes: Sequence[str] = ()
    excludes: Sequence[str] = ()
    rollback: str = ""
    context_detail: str = ""

    def __post_init__(self) -> None:
        for field_name in (
            "decision_id", "title", "state", "target", "decision", "default_action"
        ):
            if not sanitize_visible_text(getattr(self, field_name)):
                raise ValueError(f"{field_name} is required")
        if sum(1 for choice in self.choices if choice.recommended) > 1:
            raise ValueError("only one choice may be recommended")
        kind = select_surface_kind(self)
        if kind in {SurfaceKind.COMPLEX, SurfaceKind.RISK, SurfaceKind.APPROVAL}:
            if not sanitize_visible_text(self.context):
                raise ValueError("context is required for complex decisions")
            if not tuple(item for item in self.established if sanitize_visible_text(item)):
                raise ValueError("established facts are required for complex decisions")
        if kind in {SurfaceKind.RISK, SurfaceKind.APPROVAL}:
            if not sanitize_visible_text(self.risk):
                raise ValueError("risk is required for risk decisions")
            if not tuple(item for item in self.includes if sanitize_visible_text(item)):
                raise ValueError("includes is required for risk decisions")
            if not tuple(item for item in self.excludes if sanitize_visible_text(item)):
                raise ValueError("excludes is required for risk decisions")
            if not sanitize_visible_text(self.rollback):
                raise ValueError("rollback is required for risk decisions")


def select_surface_kind(request: DecisionRequest) -> SurfaceKind:
    if request.kind is not None:
        return SurfaceKind(request.kind)
    if request.risk or request.includes or request.excludes or request.rollback:
        return SurfaceKind.RISK
    if request.context or request.established or request.recommendation:
        return SurfaceKind.COMPLEX
    return SurfaceKind.SIMPLE


def _list_lines(values: Iterable[object]) -> list[str]:
    return [f"- {sanitize_visible_text(value)}" for value in values if sanitize_visible_text(value)]


def _essential_blocks(request: DecisionRequest) -> list[str]:
    blocks = [
        sanitize_visible_text(request.title),
        sanitize_visible_text(request.state),
        f"TARGET\n{sanitize_visible_text(request.target)}",
        f"DECISION\n{sanitize_visible_text(request.decision)}",
    ]
    if request.choices:
        choice_lines = []
        for choice in request.choices:
            recommendation = " · RECOMMENDED" if choice.recommended else ""
            choice_lines.append(
                f"- {sanitize_visible_text(choice.label)} — "
                f"{sanitize_visible_text(choice.consequence)}{recommendation}"
            )
        blocks.append("CHOICES\n" + "\n".join(choice_lines))
    if sanitize_visible_text(request.recommendation):
        blocks.append("RECOMMENDATION\n" + sanitize_visible_text(request.recommendation))
    if sanitize_visible_text(request.risk):
        blocks.append("RISK\n" + sanitize_visible_text(request.risk))
    if request.includes:
        blocks.append("INCLUDES\n" + "\n".join(_list_lines(request.includes)))
    if request.excludes:
        blocks.append("EXCLUDES\n" + "\n".join(_list_lines(request.excludes)))
    if sanitize_visible_text(request.rollback):
        blocks.append("ROLLBACK\n" + sanitize_visible_text(request.rollback))
    blocks.append("DEFAULT\n" + sanitize_visible_text(request.default_action))
    return blocks


def render_compact_clarify_content(
    request: DecisionRequest, limit: int = 2000
) -> str:
    """Render one concise question surface; controls carry choice detail."""
    blocks = [
        f"**{sanitize_visible_text(request.title)}**",
        sanitize_visible_text(request.state),
        sanitize_visible_text(request.decision),
    ]
    risk = sanitize_visible_text(request.risk)
    if risk:
        blocks.append(f"Risk: {risk}")
    blocks.append(f"If unanswered: {sanitize_visible_text(request.default_action)}")
    return _prefix_utf16("\n\n".join(block for block in blocks if block), limit)


@dataclass(frozen=True)
class RenderedDecisionEmbed:
    title: str
    description: str
    semantic_color: str


def build_decision_embed(request: DecisionRequest, limit: int = 4096) -> RenderedDecisionEmbed:
    """Return adapter-neutral embed fields without importing discord.py."""
    rendered = render_decision_content(request, limit=limit)
    _first, separator, remainder = rendered.partition("\n\n")
    kind = select_surface_kind(request)
    semantic_color = "warning" if kind in {SurfaceKind.RISK, SurfaceKind.APPROVAL} else "neutral"
    return RenderedDecisionEmbed(
        title=sanitize_visible_text(request.title),
        description=remainder if separator else rendered,
        semantic_color=semantic_color,
    )


def render_decision_content(request: DecisionRequest, limit: int = 2000) -> str:
    """Render a decision while preserving actionable information before context."""
    if limit < 1:
        return ""
    essential = "\n\n".join(_essential_blocks(request))
    context_parts = []
    if sanitize_visible_text(request.context):
        context_parts.append("CONTEXT\n" + sanitize_visible_text(request.context))
    established = _list_lines(request.established)
    if established:
        context_parts.append("ESTABLISHED\n" + "\n".join(established))
    if sanitize_visible_text(request.context_detail):
        context_parts.append("DETAIL\n" + sanitize_visible_text(request.context_detail))

    if not context_parts:
        return _prefix_utf16(essential, limit)

    context = "\n\n".join(context_parts)
    full = f"{essential}\n\n{context}"
    if utf16_len(full) <= limit:
        return full

    marker = "\n\nCONTEXT\n[Context shortened — use Context for full evidence]"
    available = limit - utf16_len(essential) - utf16_len(marker) - utf16_len("\n\n")
    if available <= 0:
        return _prefix_utf16(essential, limit)
    shortened = _prefix_utf16(context, available).rstrip()
    return f"{essential}\n\n{shortened}{marker}"
