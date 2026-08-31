"""Deterministic, platform-safe Discord interaction surfaces.

This module contains no gateway state and no discord.py dependency.  It turns a
validated decision contract into bounded visible content; adapter Views own the
interactive controls and authorization lifecycle.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from threading import Lock
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


def _truncate_utf16(value: str, limit: int, marker: str = "…") -> str:
    if utf16_len(value) <= limit:
        return value
    marker = _prefix_utf16(marker, limit)
    return _prefix_utf16(value, max(0, limit - utf16_len(marker))).rstrip() + marker


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
    source_session: str = ""
    expires_at: Optional[datetime] = None
    batch_items: Sequence["DecisionRequest"] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "decision_id", "title", "state", "target", "decision", "default_action"
        ):
            if not sanitize_visible_text(getattr(self, field_name)):
                raise ValueError(f"{field_name} is required")
        if sum(1 for choice in self.choices if choice.recommended) > 1:
            raise ValueError("only one choice may be recommended")
        if self.expires_at is not None and (
            self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None
        ):
            raise ValueError("expires_at must be timezone-aware")
        kind = select_surface_kind(self)
        if kind is SurfaceKind.BATCH:
            if not 2 <= len(self.batch_items) <= 5:
                raise ValueError("batch decisions require two to five independent questions")
            ids = [item.decision_id for item in self.batch_items]
            if len(set(ids)) != len(ids) or any(
                select_surface_kind(item) is SurfaceKind.BATCH for item in self.batch_items
            ):
                raise ValueError("batch questions must be independent and uniquely identified")
            if any(item.source_session != self.source_session for item in self.batch_items):
                raise ValueError("batch questions must bind to the same source session")
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


def decision_request_from_clarify(
    *,
    question: str,
    choices: Sequence[str],
    clarify_id: str,
    source_session: str,
    surface: Optional[dict],
) -> DecisionRequest:
    """Parse the typed boundary or safely adapt a legacy clarify call."""
    question = sanitize_visible_text(question)
    if not question:
        raise ValueError("clarify question is required")
    if surface is None:
        title = _truncate_utf16(question.splitlines()[0].strip(" #*?"), 80)
        parsed_choices = []
        for index, raw_label in enumerate(choices):
            label = sanitize_visible_text(raw_label)
            recommended = label.casefold().endswith("(recommended)")
            if recommended:
                label = label[: -len("(recommended)")].rstrip()
            parsed_choices.append(
                DecisionChoice(
                    id=f"choice-{index + 1}",
                    label=label,
                    consequence=f"Choose {label}.",
                    recommended=recommended,
                )
            )
        return DecisionRequest(
            decision_id=str(clarify_id),
            kind=SurfaceKind.SIMPLE if parsed_choices else SurfaceKind.OPEN_TEXT,
            title=title or "Decision required",
            state="Work is paused for this answer.",
            target="Current request in this session",
            decision=question,
            choices=tuple(parsed_choices),
            default_action="No action; work remains paused until answered.",
            source_session=str(source_session),
        )

    raw_choices = surface.get("choices")
    consequences = tuple(surface.get("consequences") or ())
    parsed_choices = []
    if isinstance(raw_choices, (list, tuple)) and raw_choices:
        for index, raw in enumerate(raw_choices):
            if not isinstance(raw, dict):
                raise ValueError("typed decision choices must be objects")
            parsed_choices.append(
                DecisionChoice(
                    id=str(raw.get("id") or f"choice-{index + 1}"),
                    label=str(raw.get("label") or ""),
                    consequence=str(raw.get("consequence") or ""),
                    recommended=bool(raw.get("recommended", False)),
                    reason=str(raw.get("reason") or ""),
                )
            )
    else:
        for index, raw_label in enumerate(choices):
            label = sanitize_visible_text(raw_label)
            recommended = label.casefold().endswith("(recommended)")
            if recommended:
                label = label[: -len("(recommended)")].rstrip()
            consequence = str(consequences[index]) if index < len(consequences) else ""
            parsed_choices.append(
                DecisionChoice(
                    id=f"choice-{index + 1}",
                    label=label,
                    consequence=consequence,
                    recommended=recommended,
                )
            )
    raw_expiry = surface.get("expires_at")
    expires_at = None
    if raw_expiry:
        expires_at = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
    raw_kind = surface.get("kind")
    kind = SurfaceKind(raw_kind) if raw_kind else (
        SurfaceKind.RISK
        if any(surface.get(name) for name in ("risk", "includes", "excludes", "rollback"))
        else SurfaceKind.COMPLEX
        if any(surface.get(name) for name in ("context", "established", "recommendation"))
        else SurfaceKind.SIMPLE if parsed_choices else SurfaceKind.OPEN_TEXT
    )
    return DecisionRequest(
        decision_id=str(surface.get("decision_id") or clarify_id),
        kind=kind,
        title=str(surface.get("title") or _truncate_utf16(question, 80)),
        state=str(surface.get("state") or ""),
        context=str(surface.get("context") or ""),
        established=tuple(surface.get("established") or ()),
        target=str(surface.get("target") or ""),
        decision=str(surface.get("decision") or question),
        choices=tuple(parsed_choices),
        recommendation=str(surface.get("recommendation") or ""),
        risk=str(surface.get("risk") or ""),
        includes=tuple(surface.get("includes") or ()),
        excludes=tuple(surface.get("excludes") or ()),
        rollback=str(surface.get("rollback") or ""),
        default_action=str(surface.get("default_action") or ""),
        context_detail=str(surface.get("context_detail") or ""),
        source_session=str(surface.get("source_session") or source_session),
        expires_at=expires_at,
    )


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
        _truncate_utf16(sanitize_visible_text(request.title), 120),
        _truncate_utf16(sanitize_visible_text(request.state), 160),
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


@dataclass(frozen=True)
class RenderedDecisionSurface:
    mode: str
    title: str
    body: str
    semantic_color: str
    primary_label: str
    detail_label: str
    cancel_label: str


@dataclass(frozen=True)
class RenderedScopeConfirmation:
    body: str
    ephemeral: bool
    confirm_custom_id: str
    cancel_custom_id: str


@dataclass(frozen=True)
class RenderedTextModal:
    title: str
    input_label: str
    placeholder: str
    custom_id: str


@dataclass(frozen=True)
class ComponentOption:
    value: str
    label: str
    description: str
    recommended: bool = False


@dataclass(frozen=True)
class ComponentSpec:
    kind: str
    custom_id: str
    label: str = ""
    options: Sequence[ComponentOption] = ()


@dataclass(frozen=True)
class AuthorizationBinding:
    user_ids: frozenset[str]
    role_ids: frozenset[str]
    guild_id: str
    channel_id: str
    profile_id: str
    target: str


@dataclass(frozen=True)
class CallbackContext:
    user_id: str
    role_ids: frozenset[str]
    guild_id: str
    channel_id: str
    profile_id: str
    target: str


@dataclass(frozen=True)
class ResolutionResult:
    status: str
    selected_value: str = ""


@dataclass(frozen=True)
class DecisionSnapshot:
    status: str
    selected_value: str
    controls_disabled: bool
    default_action: str
    source_session: str


class DecisionLifecycle:
    """Process-local atomic state machine used by Discord callbacks."""

    def __init__(self, request: DecisionRequest, binding: AuthorizationBinding):
        self.request = request
        self.binding = binding
        self._status = "unresolved"
        self._selected_value = ""
        self._lock = Lock()

    def _authorized(self, context: CallbackContext) -> bool:
        identity_allowed = (
            context.user_id in self.binding.user_ids
            or bool(context.role_ids & self.binding.role_ids)
        )
        return identity_allowed and (
            context.guild_id == self.binding.guild_id
            and context.channel_id == self.binding.channel_id
            and context.profile_id == self.binding.profile_id
            and context.target == self.binding.target == self.request.target
        )

    def resolve(
        self, context: CallbackContext, value: str, *, now: datetime
    ) -> ResolutionResult:
        if not self._authorized(context):
            return ResolutionResult("unauthorized")
        with self._lock:
            if self._status == "resolved":
                return ResolutionResult("already_resolved", self._selected_value)
            if self._status == "expired" or (
                self.request.expires_at is not None and now >= self.request.expires_at
            ):
                self._status = "expired"
                return ResolutionResult("expired")
            self._status = "resolved"
            self._selected_value = str(value)
            return ResolutionResult("accepted", self._selected_value)

    def snapshot(self) -> DecisionSnapshot:
        with self._lock:
            return DecisionSnapshot(
                status=self._status,
                selected_value=self._selected_value,
                controls_disabled=self._status != "unresolved",
                default_action=self.request.default_action,
                source_session=self.request.source_session,
            )


def build_component_blueprint(request: DecisionRequest) -> tuple[ComponentSpec, ...]:
    """Build deterministic native controls without importing discord.py."""
    rendered = render_decision_surface(request)
    prefix = f"decision:{request.decision_id}"
    controls = []
    if request.choices:
        controls.append(
            ComponentSpec(
                kind="select",
                custom_id=f"{prefix}:select",
                options=tuple(
                    ComponentOption(
                        value=choice.id,
                        label=choice.label,
                        description=choice.consequence,
                        recommended=choice.recommended,
                    )
                    for choice in request.choices[:25]
                ),
            )
        )
    controls.extend(
        (
            ComponentSpec("button", f"{prefix}:confirm", rendered.primary_label),
            ComponentSpec("button", f"{prefix}:context", rendered.detail_label),
            ComponentSpec("button", f"{prefix}:close", rendered.cancel_label),
        )
    )
    return tuple(controls)


def render_open_text_modal(request: DecisionRequest) -> RenderedTextModal:
    if select_surface_kind(request) is not SurfaceKind.OPEN_TEXT:
        raise ValueError("text modal requires an open_text decision")
    return RenderedTextModal(
        title="Write response",
        input_label="Response",
        placeholder="Type the information needed to continue",
        custom_id=f"decision:{request.decision_id}:text",
    )


def render_exact_scope_confirmation(request: DecisionRequest) -> RenderedScopeConfirmation:
    """Render the private second stage required by risk and approval decisions."""
    kind = select_surface_kind(request)
    if kind not in {SurfaceKind.RISK, SurfaceKind.APPROVAL}:
        raise ValueError("exact-scope confirmation requires a risk or approval decision")
    body = "\n\n".join(
        (
            "INCLUDES\n" + "\n".join(_list_lines(request.includes)),
            "EXCLUDES\n" + "\n".join(_list_lines(request.excludes)),
            "Confirm this exact scope. No other profile or target is authorized.",
        )
    )
    prefix = f"decision:{request.decision_id}"
    return RenderedScopeConfirmation(
        body=body,
        ephemeral=True,
        confirm_custom_id=f"{prefix}:approve",
        cancel_custom_id=f"{prefix}:cancel",
    )


def render_decision_surface(request: DecisionRequest) -> RenderedDecisionSurface:
    """Render the visible information hierarchy and action labels for a decision."""
    kind = select_surface_kind(request)
    blocks = [sanitize_visible_text(request.state)]
    if kind is SurfaceKind.BATCH:
        if sanitize_visible_text(request.context):
            blocks.append("CONTEXT\n" + sanitize_visible_text(request.context))
        established = _list_lines(request.established)
        if established:
            blocks.append("ESTABLISHED\n" + "\n".join(established))
        for index, item in enumerate(request.batch_items, start=1):
            choices = "\n".join(
                f"- {sanitize_visible_text(choice.label)} — "
                f"{sanitize_visible_text(choice.consequence)}"
                for choice in item.choices
            )
            blocks.append(
                f"QUESTION {index}\n{sanitize_visible_text(item.decision)}\n"
                f"TARGET · {sanitize_visible_text(item.target)}\n{choices}"
            )
        blocks.append(f"DEFAULT\n{sanitize_visible_text(request.default_action)}")
        return RenderedDecisionSurface(
            mode="embed",
            title=sanitize_visible_text(request.title),
            body="\n\n".join(blocks),
            semantic_color="neutral",
            primary_label="Continue",
            detail_label="Technical context",
            cancel_label="Close",
        )
    if kind is not SurfaceKind.SIMPLE:
        blocks.append("CONTEXT\n" + sanitize_visible_text(request.context))
        blocks.append("ESTABLISHED\n" + "\n".join(_list_lines(request.established)))
    blocks.append(f"TARGET\n{sanitize_visible_text(request.target)}")
    decision_heading = (
        "CHANGE" if kind in {SurfaceKind.RISK, SurfaceKind.APPROVAL} else "DECISION"
    )
    blocks.append(f"{decision_heading}\n{sanitize_visible_text(request.decision)}")
    if kind is not SurfaceKind.SIMPLE:
        recommendation = sanitize_visible_text(request.recommendation)
        if recommendation:
            blocks.append("RECOMMENDATION\n" + recommendation)
        if request.choices:
            blocks.append(
                "CHOICES\n"
                + "\n".join(
                    f"- {sanitize_visible_text(choice.label)} — "
                    f"{sanitize_visible_text(choice.consequence)}"
                    for choice in request.choices
                )
            )
    if kind in {SurfaceKind.RISK, SurfaceKind.APPROVAL}:
        blocks.extend(
            (
                "RISK\n" + sanitize_visible_text(request.risk),
                "INCLUDES\n" + "\n".join(_list_lines(request.includes)),
                "EXCLUDES\n" + "\n".join(_list_lines(request.excludes)),
                "ROLLBACK\n" + sanitize_visible_text(request.rollback),
            )
        )
    blocks.append(f"DEFAULT\n{sanitize_visible_text(request.default_action)}")
    complex_surface = kind in {
        SurfaceKind.COMPLEX, SurfaceKind.RISK, SurfaceKind.APPROVAL, SurfaceKind.BATCH
    }
    return RenderedDecisionSurface(
        mode="content" if not complex_surface else "embed",
        title=sanitize_visible_text(request.title),
        body="\n\n".join(block for block in blocks if block),
        semantic_color=(
            "warning" if kind in {SurfaceKind.RISK, SurfaceKind.APPROVAL} else "neutral"
        ),
        primary_label=(
            "Review & approve"
            if kind in {SurfaceKind.RISK, SurfaceKind.APPROVAL}
            else "Write response" if kind is SurfaceKind.OPEN_TEXT
            else "Continue" if complex_surface else "Confirm"
        ),
        detail_label=(
            "Evidence"
            if kind in {SurfaceKind.RISK, SurfaceKind.APPROVAL}
            else "Technical context" if complex_surface else "Context"
        ),
        cancel_label=(
            "Cancel" if kind in {SurfaceKind.RISK, SurfaceKind.APPROVAL} else "Close"
        ),
    )


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
    """Render exact hierarchy, falling back to action-first UTF-16 truncation."""
    if limit < 1:
        return ""
    surface = render_decision_surface(request)
    primary = f"{surface.title}\n\n{surface.body}"
    detail = sanitize_visible_text(request.context_detail)
    full = primary + (f"\n\nDETAIL\n{detail}" if detail else "")
    if utf16_len(full) <= limit:
        return full
    if utf16_len(primary) <= limit:
        marker = "\n\nDETAIL\n[Evidence shortened — use the detail control]"
        available = limit - utf16_len(primary) - utf16_len(marker)
        if available <= 0:
            return _prefix_utf16(primary, limit)
        return f"{primary}\n\n{_prefix_utf16(detail, available).rstrip()}{marker}"

    # Oversized visible copy is rebuilt in preservation order so long title,
    # state, and context can never push out target, decision, choices, risk, or
    # the safe default.
    essential = "\n\n".join(_essential_blocks(request))
    if utf16_len(essential) <= limit:
        return essential
    return _prefix_utf16(essential, limit)
