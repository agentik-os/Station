"""Concise, public-safe operational status surfaces for Discord."""
from __future__ import annotations

from enum import Enum

from .interaction_surfaces import sanitize_visible_text


class StatusKind(str, Enum):
    BLOCKED = "blocked"
    FAILED = "failed"
    STALE = "stale"
    EXPIRED = "expired"
    RETRYING = "retrying"
    RECOVERED = "recovered"
    COMPLETE = "complete"


_DIAGNOSTIC_MARKERS = (
    "traceback (most recent call last)",
    "stack trace",
    "provider body",
    "response body",
)
_PRIVATE_DIAGNOSTIC_STATE = "Detailed diagnostics were recorded privately."


def _required(field_name: str, value: object) -> str:
    sanitized = sanitize_visible_text(value)
    if not sanitized:
        raise ValueError(f"{field_name} is required")
    return sanitized


def _public_state(value: object) -> str:
    raw = str(value or "").strip()
    lowered = raw.lower()
    if any(marker in lowered for marker in _DIAGNOSTIC_MARKERS):
        return _PRIVATE_DIAGNOSTIC_STATE
    return _required("state", raw)


def render_status(
    kind: StatusKind | str,
    title: object,
    state: object,
    target: object,
    next_action: object,
    reference: object | None = None,
) -> str:
    """Render one bounded operational state without leaking diagnostics.

    The visible contract is deliberately plain text: semantic state is named,
    not decorated with warning colors or icons. Detailed exception/provider
    output belongs in logs and evidence, never in a public Discord response.
    """
    resolved_kind = StatusKind(kind)
    blocks = [
        _required("title", title),
        f"STATE · {resolved_kind.value.upper()}\n{_public_state(state)}",
        f"TARGET\n{_required('target', target)}",
        f"NEXT\n{_required('next_action', next_action)}",
    ]
    if reference is not None and sanitize_visible_text(reference):
        blocks.append(f"REFERENCE\n{sanitize_visible_text(reference)}")
    return "\n\n".join(blocks)
