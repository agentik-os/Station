"""Discord notification classification, deduplication, and pacing."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional


class NotificationKind(str, Enum):
    PROGRESS = "progress"
    RETRY = "retry"
    COMPRESSION_WARNING = "compression_warning"
    RECOVERY = "recovery"
    GATEWAY_LIFECYCLE = "gateway_lifecycle"
    FAILURE = "failure"


class NotificationAction(str, Enum):
    SEND = "send"
    EDIT = "edit"
    SUPPRESS = "suppress"


@dataclass(frozen=True)
class NotificationDecision:
    action: NotificationAction
    kind: Optional[NotificationKind] = None
    event_key: Optional[str] = None
    incident_id: Optional[str] = None
    message_id: Optional[str] = None
    reason: str = ""


def stable_event_key(kind: NotificationKind | str, target: object, incident_id: object = "") -> str:
    """Build a stable opaque key without exposing target or incident details."""
    kind_value = kind.value if isinstance(kind, NotificationKind) else str(kind).strip().lower()
    material = f"{kind_value}\0{target!s}\0{incident_id!s}".encode("utf-8", errors="replace")
    digest = hashlib.sha256(material).hexdigest()[:20]
    return f"{kind_value}:{digest}"


class DiscordNotificationPolicy:
    """Own one visible notification per incident and one message per progress key."""

    _LIFECYCLE_PREFIXES = (
        "♻️ gateway online",
        "♻ gateway online",
        "♻️ gateway restarted",
        "♻ gateway restarted",
    )

    def __init__(self, *, owner_recovery_notifications: bool = False):
        self.owner_recovery_notifications = bool(owner_recovery_notifications)
        self._message_ids: dict[str, str] = {}
        self._active_incidents: set[str] = set()
        self._resolved_incidents: set[str] = set()
        self._seen_events: set[str] = set()
        self._reserved: set[str] = set()

    def note_incident(self, incident_id: object) -> None:
        incident = str(incident_id or "").strip()
        if incident:
            self._active_incidents.add(incident)
            self._resolved_incidents.discard(incident)

    def decide(self, content: object, metadata: Optional[Mapping[str, Any]]) -> NotificationDecision:
        data = metadata if isinstance(metadata, Mapping) else {}
        text = str(content or "").strip()
        lowered = text.lower()

        if any(lowered.startswith(prefix) for prefix in self._LIFECYCLE_PREFIXES):
            return NotificationDecision(
                NotificationAction.SUPPRESS,
                kind=NotificationKind.GATEWAY_LIFECYCLE,
                reason="gateway lifecycle notices stay out of Discord channels",
            )

        raw_kind = str(data.get("notification_kind") or "").strip().lower()
        if not raw_kind:
            return NotificationDecision(NotificationAction.SEND)
        if raw_kind in {"tool", "tool_call", "tool_progress", "tool_chatter"}:
            return NotificationDecision(
                NotificationAction.SUPPRESS,
                reason="tool-call chatter is not a user notification",
            )
        if bool(data.get("bot_generated") or data.get("author_is_bot")):
            return NotificationDecision(
                NotificationAction.SUPPRESS,
                reason="bot-generated notifications cannot trigger notification loops",
            )

        try:
            kind = NotificationKind(raw_kind)
        except ValueError:
            return NotificationDecision(
                NotificationAction.SUPPRESS,
                reason="unclassified notifications fail closed",
            )

        target = data.get("notification_target", "discord")
        incident = str(data.get("notification_incident_id") or "").strip() or None
        event_key = str(data.get("notification_event_key") or "").strip()
        if not event_key:
            event_key = stable_event_key(kind, target, incident or "")

        if kind is NotificationKind.GATEWAY_LIFECYCLE:
            return NotificationDecision(
                NotificationAction.SUPPRESS, kind=kind, event_key=event_key,
                reason="gateway lifecycle notices stay out of Discord channels",
            )

        if kind is NotificationKind.PROGRESS:
            existing = self._message_ids.get(event_key)
            if existing:
                return NotificationDecision(
                    NotificationAction.EDIT, kind, event_key, incident, existing,
                )
            if event_key in self._reserved:
                return NotificationDecision(
                    NotificationAction.SUPPRESS, kind, event_key, incident,
                    reason="progress send already in flight",
                )
            self._reserved.add(event_key)
            return NotificationDecision(NotificationAction.SEND, kind, event_key, incident)

        if kind is NotificationKind.RECOVERY:
            if not (
                self.owner_recovery_notifications
                and bool(data.get("material_recovery"))
                and bool(data.get("owner_dm"))
            ):
                return NotificationDecision(
                    NotificationAction.SUPPRESS, kind, event_key, incident,
                    reason="recovery is only routed to a configured owner DM",
                )
            if not incident or incident not in self._active_incidents:
                return NotificationDecision(
                    NotificationAction.SUPPRESS, kind, event_key, incident,
                    reason="recovery has no active incident",
                )
            recovery_key = f"recovery:{incident}"
            if incident in self._resolved_incidents or recovery_key in self._reserved:
                return NotificationDecision(
                    NotificationAction.SUPPRESS, kind, event_key, incident,
                    reason="recovery was already reported",
                )
            self._reserved.add(recovery_key)
            return NotificationDecision(NotificationAction.SEND, kind, event_key, incident)

        if kind in {
            NotificationKind.FAILURE,
            NotificationKind.RETRY,
            NotificationKind.COMPRESSION_WARNING,
        }:
            dedup_key = f"incident:{incident}" if incident else event_key
            if dedup_key in self._reserved or (
                incident in self._active_incidents if incident else event_key in self._seen_events
            ):
                return NotificationDecision(
                    NotificationAction.SUPPRESS, kind, event_key, incident,
                    reason="incident notification already emitted",
                )
            self._reserved.add(dedup_key)
            return NotificationDecision(NotificationAction.SEND, kind, event_key, incident)

        return NotificationDecision(NotificationAction.SEND, kind, event_key, incident)

    def record(self, decision: NotificationDecision, *, success: bool, message_id: object = None) -> None:
        """Commit or release a decision after the Discord operation completes."""
        if decision.kind is NotificationKind.PROGRESS and decision.event_key:
            self._reserved.discard(decision.event_key)
            if success and message_id is not None:
                self._message_ids[decision.event_key] = str(message_id)
            return

        if decision.kind is NotificationKind.RECOVERY and decision.incident_id:
            reservation = f"recovery:{decision.incident_id}"
            self._reserved.discard(reservation)
            if success:
                self._active_incidents.discard(decision.incident_id)
                self._resolved_incidents.add(decision.incident_id)
            return

        if decision.kind in {
            NotificationKind.FAILURE,
            NotificationKind.RETRY,
            NotificationKind.COMPRESSION_WARNING,
        }:
            dedup_key = (
                f"incident:{decision.incident_id}"
                if decision.incident_id
                else decision.event_key
            )
            if dedup_key:
                self._reserved.discard(dedup_key)
            if not success:
                return
            if decision.incident_id:
                self._active_incidents.add(decision.incident_id)
                self._resolved_incidents.discard(decision.incident_id)
            elif decision.event_key:
                self._seen_events.add(decision.event_key)
