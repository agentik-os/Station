"""Canonical, redacted account roster and owner alias registry."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent.account_usage import fetch_account_usage
from agent.credential_pool import load_pool
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

logger = logging.getLogger(__name__)

_PROVIDER_LABELS = {"openai-codex": "OpenAI", "anthropic": "Claude"}
_PROVIDERS = tuple(_PROVIDER_LABELS)
_SAFE_STATUSES = {"ok", "exhausted", "dead", "reconnect required"}
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_JWT = re.compile(r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\Z")
_SECRET_PREFIXES = ("sk-", "xox", "ghp_", "github_pat_", "aiza", "akia", "bearer ")


def _normalize_provider(value: object) -> str:
    provider = str(value).strip()
    if provider not in _PROVIDER_LABELS:
        raise ValueError("provider must be canonical")
    return provider


def _normalize_credential_id(value: object) -> str:
    credential_id = str(value).strip()
    folded = credential_id.casefold()
    looks_secret = (
        _JWT.fullmatch(credential_id) is not None
        or folded.startswith(_SECRET_PREFIXES)
        or len(credential_id) >= 48
    )
    if not _SAFE_ID.fullmatch(credential_id) or looks_secret:
        raise ValueError("credential_id must use a canonical safe format")
    return credential_id


def _normalize_owner_name(value: object) -> str:
    owner_name = str(value).strip()
    folded = owner_name.casefold()
    safe_characters = all(character.isalnum() or character in {" ", "-"} for character in owner_name)
    looks_secret = (
        _JWT.fullmatch(owner_name) is not None
        or folded.startswith(_SECRET_PREFIXES)
        or (len(owner_name) >= 32 and " " not in owner_name)
    )
    if not owner_name or len(owner_name) > 64 or not safe_characters or looks_secret:
        raise ValueError("owner_name must be a safe nickname")
    return owner_name


def _safe_usage_label(value: object) -> str:
    label = str(value).strip()
    folded = label.casefold()
    looks_secret = _JWT.fullmatch(label) is not None or folded.startswith(_SECRET_PREFIXES)
    if (
        not label
        or len(label) > 100
        or not all(character.isalnum() or character in {" ", "-", "/"} for character in label)
        or looks_secret
    ):
        return "Limit"
    return label


def _safe_reset_at(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat()


@dataclass(frozen=True)
class UsageWindow:
    label: str
    remaining_percent: float | None
    reset_at: str | None


@dataclass(frozen=True)
class AccountRecord:
    provider: str
    credential_id: str
    owner_name: str
    status: str
    priority: int
    windows: tuple[UsageWindow, ...]


class AliasRegistry:
    """Mode-0600 credential-id to owner-nickname mappings."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def snapshot(self) -> dict[str, dict[str, str]]:
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return {}
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                descriptor = None
                payload = json.load(handle)
        except (OSError, TypeError, ValueError):
            return {}
        finally:
            if descriptor is not None:
                os.close(descriptor)
        providers = payload.get("providers") if isinstance(payload, dict) else None
        if not isinstance(providers, dict):
            return {}
        result: dict[str, dict[str, str]] = {}
        for provider, rows in providers.items():
            if not isinstance(rows, list):
                continue
            try:
                provider_name = _normalize_provider(provider)
            except ValueError:
                continue
            aliases: dict[str, str] = {}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    credential_id = _normalize_credential_id(row.get("credential_id") or "")
                    owner_name = _normalize_owner_name(row.get("owner_nickname") or "")
                except ValueError:
                    continue
                aliases[credential_id] = owner_name
            if aliases:
                result[provider_name] = aliases
        return result

    def replace(self, values: dict[str, dict[str, str]]) -> None:
        normalized: dict[str, dict[str, str]] = {}
        for provider, aliases in values.items():
            provider_name = _normalize_provider(provider)
            if not isinstance(aliases, dict):
                raise TypeError("aliases must map credential IDs to owner names")
            rows: dict[str, str] = {}
            for credential_id, owner_name in aliases.items():
                stable_id = _normalize_credential_id(credential_id)
                nickname = _normalize_owner_name(owner_name)
                rows[stable_id] = nickname
            if rows:
                normalized[provider_name] = rows
        payload = {
            "providers": {
                provider: [
                    {"credential_id": credential_id, "owner_nickname": owner_name}
                    for credential_id, owner_name in sorted(aliases.items())
                ]
                for provider, aliases in sorted(normalized.items())
            }
        }
        self._write(payload)

    def bind(self, provider: str, owner_name: str, credential_id: str) -> None:
        provider = _normalize_provider(provider)
        owner_name = _normalize_owner_name(owner_name)
        credential_id = _normalize_credential_id(credential_id)
        aliases = self.snapshot()
        provider_aliases = aliases.setdefault(provider, {})
        owner_key = owner_name.casefold()
        provider_aliases = {
            stable_id: nickname
            for stable_id, nickname in provider_aliases.items()
            if nickname.casefold() != owner_key and stable_id != credential_id
        }
        provider_aliases[credential_id] = owner_name
        aliases[provider] = provider_aliases
        self.replace(aliases)

    def remove_credential(self, provider: str, credential_id: str) -> None:
        aliases = self.snapshot()
        provider_aliases = aliases.get(provider)
        if not provider_aliases or credential_id not in provider_aliases:
            return
        del provider_aliases[credential_id]
        if not provider_aliases:
            aliases.pop(provider, None)
        self.replace(aliases)

    def owner_name(self, provider: str, credential_id: str) -> str | None:
        return self.snapshot().get(provider, {}).get(credential_id)

    def credential_id(self, provider: str, owner_name: str) -> str | None:
        wanted = owner_name.casefold()
        return next(
            (
                credential_id
                for credential_id, nickname in self.snapshot().get(provider, {}).items()
                if nickname.casefold() == wanted
            ),
            None,
        )

    def _write(self, payload: dict) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".new", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _usage_windows(provider: str, entry) -> tuple[UsageWindow, ...]:
    token = getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", None)
    if not token:
        return ()
    try:
        usage = fetch_account_usage(
            provider,
            base_url=getattr(entry, "base_url", None),
            api_key=token,
        )
    except Exception as exc:  # noqa: BLE001 - provider clients expose no stable error base.
        logger.warning("Account usage fetch failed safely for %s: %s", provider, type(exc).__name__)
        return ()
    windows: list[UsageWindow] = []
    for window in (getattr(usage, "windows", ()) or ()):
        used = getattr(window, "used_percent", None)
        remaining = None if used is None else max(0.0, min(100.0, 100.0 - float(used)))
        reset_at = getattr(window, "reset_at", None)
        windows.append(
            UsageWindow(
                label=str(getattr(window, "label", None) or "Limit")[:100],
                remaining_percent=remaining,
                reset_at=reset_at.isoformat() if hasattr(reset_at, "isoformat") else None,
            )
        )
    return tuple(windows)


def load_account_roster(
    hermes_home: Path, *, pool_loader=load_pool
) -> tuple[AccountRecord, ...]:
    aliases = AliasRegistry(Path(hermes_home) / "provider-account-aliases.json").snapshot()
    records: list[AccountRecord] = []
    token = set_hermes_home_override(hermes_home)
    try:
        for provider in _PROVIDERS:
            pool = pool_loader(provider)
            for entry in pool.entries():
                raw_status = str(getattr(entry, "last_status", None) or "").lower()
                status = raw_status if raw_status in _SAFE_STATUSES else "unknown"
                windows = _usage_windows(provider, entry)
                if windows and status == "unknown":
                    status = "ok"
                credential_id = str(getattr(entry, "id", None) or "unknown")[:64]
                records.append(
                    AccountRecord(
                        provider=provider,
                        credential_id=credential_id,
                        owner_name=aliases.get(provider, {}).get(credential_id, ""),
                        status=status,
                        priority=int(getattr(entry, "priority", 0) or 0),
                        windows=windows,
                    )
                )
    finally:
        reset_hermes_home_override(token)
    return tuple(sorted(records, key=lambda record: (_PROVIDERS.index(record.provider), record.priority)))


def render_account_roster(records: Iterable[AccountRecord]) -> str:
    """Render only explicitly whitelisted, non-secret account fields."""
    lines = ["# Station · Account roster", ""]
    for record in records:
        try:
            owner = _normalize_owner_name(record.owner_name) if record.owner_name else "Unassigned"
        except ValueError:
            owner = "Unassigned"
        provider_label = _PROVIDER_LABELS.get(record.provider, "Unknown provider")
        try:
            credential_id = _normalize_credential_id(record.credential_id)
        except ValueError:
            credential_id = "unknown"
        status = record.status if record.status in _SAFE_STATUSES else "unknown"
        priority = record.priority if isinstance(record.priority, int) and 0 <= record.priority <= 9999 else 0
        lines.append(
            f"**{owner}** · `{provider_label}` · `{credential_id}` · "
            f"`{status}` · priority {priority}"
        )
        if not record.windows:
            lines.append("- Usage unavailable")
        for window in record.windows[:3]:
            raw_remaining = window.remaining_percent
            try:
                remaining = math.nan if raw_remaining is None else float(raw_remaining)
            except (TypeError, ValueError):
                remaining = math.nan
            if not math.isfinite(remaining):
                usage = "unavailable"
            else:
                remaining = max(0.0, min(100.0, remaining))
                usage = f"{round(100.0 - remaining)}% used · {round(remaining)}% remaining"
            reset_at = _safe_reset_at(window.reset_at)
            reset = f" · resets {reset_at}" if reset_at else ""
            lines.append(f"- {_safe_usage_label(window.label)} · {usage}{reset}")
        lines.append("")
    return "\n".join(lines)


def voice_binding_key(provider: str, owner_name: str) -> str:
    return f"voice-owner:{provider}:{owner_name.casefold()}"
