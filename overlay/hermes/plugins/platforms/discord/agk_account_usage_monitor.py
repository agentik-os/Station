"""Lightweight redacted OpenAI/Claude quota panels for Discord."""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import importlib.util
import json
import logging
import os
import re
import sys
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


try:
    from .agk_account_control import (
        AliasRegistry,
        _safe_usage_label,
        voice_binding_key,
    )
except ImportError:  # Support direct file loading in focused tests.
    _CONTROL_PATH = Path(__file__).with_name("agk_account_control.py")
    _CONTROL_SPEC = importlib.util.spec_from_file_location("agk_account_control", _CONTROL_PATH)
    assert _CONTROL_SPEC and _CONTROL_SPEC.loader
    _CONTROL_MODULE = importlib.util.module_from_spec(_CONTROL_SPEC)
    sys.modules.setdefault(_CONTROL_SPEC.name, _CONTROL_MODULE)
    _CONTROL_SPEC.loader.exec_module(_CONTROL_MODULE)
    AliasRegistry = _CONTROL_MODULE.AliasRegistry
    _safe_usage_label = _CONTROL_MODULE._safe_usage_label
    voice_binding_key = _CONTROL_MODULE.voice_binding_key


@dataclass(frozen=True)
class UsageWindow:
    label: str
    remaining_percent: float | None
    reset_at: str | None


@dataclass(frozen=True)
class AccountSnapshot:
    index: int
    credential_id: str
    status: str
    windows: tuple[UsageWindow, ...] = ()
    owner_name: str = ""


@dataclass(frozen=True)
class QuotaAlert:
    key: str
    provider_label: str
    credential_id: str
    owner_name: str
    window_label: str
    used_percent: float
    reset_at: str | None


@dataclass(frozen=True)
class MonitorConfig:
    summary_or_category_id: int
    openai_channel_id: int
    interval_seconds: int = 300
    alert_channel_id: int = 0
    alert_used_threshold_percent: float = 90.0

    @classmethod
    def from_extra(cls, extra: dict | None) -> MonitorConfig:
        values = extra or {}
        interval = max(180, min(3600, int(values.get("usage_monitor_interval_seconds", 300) or 300)))
        return cls(
            summary_or_category_id=int(values.get("usage_monitor_channel_id", 0) or 0),
            openai_channel_id=int(values.get("usage_monitor_openai_channel_id", 0) or 0),
            interval_seconds=interval,
            alert_channel_id=int(values.get("usage_alert_channel_id", 0) or 0),
            alert_used_threshold_percent=max(
                1.0,
                min(100.0, float(values.get("usage_alert_used_threshold_percent", 90) or 90)),
            ),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.summary_or_category_id and self.openai_channel_id)


class MessageStateStore:
    def __init__(self, hermes_home: Path):
        self.path = Path(hermes_home) / "discord_usage_monitor.json"

    def load(self) -> dict[str, int]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(k): int(v)
            for k, v in raw.items()
            if (
                k in {"summary", "openai", "claude"}
                or str(k).startswith("voice:")
                or str(k).startswith("voice-owner:")
            )
            and isinstance(v, int)
        }

    def save(self, values: dict[str, int]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        clean = {
            str(k): int(v)
            for k, v in values.items()
            if (
                k in {"summary", "openai", "claude"}
                or str(k).startswith("voice:")
                or str(k).startswith("voice-owner:")
            )
            and isinstance(v, int)
        }
        temporary = self.path.with_name(".discord_usage_monitor.json.new")
        temporary.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)


class QuotaAlertStateStore:
    """Persist only opaque active-threshold fingerprints in the owning profile."""

    def __init__(self, hermes_home: Path):
        self.path = Path(hermes_home) / "discord_usage_alerts.json"
        self.lock_path = self.path.with_name("discord_usage_alerts.lock")

    @asynccontextmanager
    async def locked(self) -> AsyncIterator[None]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        descriptor = os.open(
            self.lock_path,
            os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        try:
            await asyncio.to_thread(fcntl.flock, descriptor, fcntl.LOCK_EX)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def load(self) -> set[str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return set()
        if not isinstance(raw, list):
            return set()
        return {
            value for value in raw
            if isinstance(value, str) and (
                (
                    len(value) == 64
                    and all(character in "0123456789abcdef" for character in value)
                )
                or (
                    len(value) == 129
                    and value[64] == ":"
                    and all(
                        character in "0123456789abcdef"
                        for character in value[:64] + value[65:]
                    )
                )
            )
        }

    def save(self, values: set[str]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        clean = sorted(
            value for value in values
            if (
                len(value) == 64
                and all(character in "0123456789abcdef" for character in value)
            ) or (
                len(value) == 129
                and value[64] == ":"
                and all(character in "0123456789abcdef" for character in value[:64] + value[65:])
            )
        )
        temporary = self.path.with_name(".discord_usage_alerts.json.new")
        temporary.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.path.chmod(0o600)


def remaining_bar(value: float | None) -> str:
    if value is None:
        return "░░░░░░░░░░"
    remaining = max(0.0, min(100.0, float(value)))
    filled = max(0, min(10, round(remaining / 10.0))) if remaining else 0
    return "█" * filled + "░" * (10 - filled)


def load_owner_aliases(hermes_home: Path) -> dict[str, dict[str, str]]:
    """Load canonical, validated stable credential-id → owner-name mappings."""
    return AliasRegistry(Path(hermes_home) / "provider-account-aliases.json").snapshot()


def _public_credential_id(value: object) -> str:
    raw = str(value or "")
    if re.fullmatch(r"[a-f0-9]{6}", raw):
        return raw
    if "@" in raw:
        return "redacted"
    return f"acct-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:8]}"


def voice_channel_name(provider_label: str, account: AccountSnapshot) -> str:
    owner = account.owner_name.strip() or f"Account-{_public_credential_id(account.credential_id)}"
    owner = "".join(ch for ch in owner if ch.isalnum() or ch in {"-", "_"}).strip("-_") or "Account"
    remaining_values = [
        max(0.0, min(100.0, float(window.remaining_percent)))
        for window in account.windows
        if window.remaining_percent is not None
    ]
    most_consumed_remaining = min(remaining_values) if remaining_values else None
    used = None if most_consumed_remaining is None else 100.0 - most_consumed_remaining
    usage = "usage unavailable" if used is None else f"{round(used)}% used"
    return f"{owner}-{provider_label} : {usage}"[:100]




def _provider_display(provider: str) -> str:
    raw = str(provider or "").strip().lower()
    if raw in {"anthropic", "claude"}:
        return "Claude"
    if raw in {"openai-codex", "codex"}:
        return "Codex"
    if raw in {"openai"}:
        return "OpenAI"
    if raw in {"openrouter"}:
        return "OpenRouter"
    return (raw or "unknown").replace("_", "-")[:24]


def _model_short(model: str) -> str:
    raw = str(model or "").strip()
    if "/" in raw:
        raw = raw.split("/")[-1]
    return raw[:40] or "unknown"


def hermes_active_channel_name(provider: str, model: str, owner_name: str | None) -> str:
    """One Discord status channel for the single active Hermes model+account."""
    owner = (owner_name or "unknown").strip()
    owner = "".join(ch for ch in owner if ch.isalnum() or ch in {"-", "_"}).strip("-_") or "unknown"
    return f"ACTIVE · {_provider_display(provider)} {_model_short(model)} · {owner}"[:100]


def load_hermes_active_model() -> tuple[str, str]:
    """Return (provider, model) from the running Hermes config — one session, one model."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        model_cfg = cfg.get("model") if isinstance(cfg, dict) else {}
        if not isinstance(model_cfg, dict):
            model_cfg = {}
        provider = str(model_cfg.get("provider") or "").strip() or "unknown"
        model = str(model_cfg.get("default") or model_cfg.get("model") or "").strip() or "unknown"
        return provider, model
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load Hermes active model safely: %s", type(exc).__name__)
        return "unknown", "unknown"


def provider_pool_key(provider: str) -> str | None:
    raw = str(provider or "").strip().lower()
    if raw in {"anthropic", "claude"}:
        return "anthropic"
    if raw in {"openai-codex", "codex"}:
        return "openai-codex"
    if raw in {"openai"}:
        return "openai"
    return None


def resolve_active_account(
    provider: str,
    rows: list[AccountSnapshot],
    aliases: dict[str, str] | None = None,
) -> AccountSnapshot | None:
    """Best-effort currently-selected credential for the active Hermes provider only."""
    by_id = {row.credential_id: row for row in rows}
    try:
        from agent.credential_pool import load_pool

        pool = load_pool(provider)
        current = pool.current() if hasattr(pool, "current") else None
        if current is not None:
            cid = str(getattr(current, "id", "") or "")
            if cid in by_id:
                return by_id[cid]
            owner = str((aliases or {}).get(cid) or getattr(current, "label", "") or "").strip()
            return AccountSnapshot(0, cid[:64], "ok", tuple(), owner)
        for entry in list(getattr(pool, "entries", lambda: [])()):
            status = str(getattr(entry, "last_status", "") or "").lower()
            if status in {"exhausted", "dead"}:
                continue
            cid = str(getattr(entry, "id", "") or "")
            if cid in by_id:
                return by_id[cid]
            owner = str((aliases or {}).get(cid) or getattr(entry, "label", "") or "").strip()
            return AccountSnapshot(0, cid[:64], status or "ok", tuple(), owner)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Active account resolve failed safely for %s: %s", provider, type(exc).__name__)
    for row in rows:
        if row.status != "dead":
            return row
    return rows[0] if rows else None


def _reset_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        epoch = int(parsed.timestamp())
        return f" · resets <t:{epoch}:F> (<t:{epoch}:R>)"
    except (TypeError, ValueError):
        return ""


def render_provider_panel(provider_label: str, accounts: Iterable[AccountSnapshot]) -> str:
    rows = list(accounts)
    lines = [f"## {provider_label} · all accounts", ""]
    if not rows:
        return "\n".join(lines + ["No account connected."])
    for account in rows:
        status = account.status if account.status in {"ok", "exhausted", "dead"} else "unknown"
        lines.append(
            f"**Account {account.index} [`{_public_credential_id(account.credential_id)}`]** · `{status}`"
        )
        if not account.windows:
            lines.append("`░░░░░░░░░░` Usage unavailable")
        for window in account.windows[:3]:
            remaining = window.remaining_percent
            if remaining is None:
                lines.append(f"`{remaining_bar(None)}` {_safe_usage_label(window.label)} · unavailable")
            else:
                remaining = max(0.0, min(100.0, float(remaining)))
                lines.append(
                    f"`{remaining_bar(remaining)}` {_safe_usage_label(window.label)} · "
                    f"{round(remaining)}% remaining{_reset_text(window.reset_at)}"
                )
        lines.append("")
    lines.append("Auto-refresh · lightweight · no credentials displayed")
    return "\n".join(lines)[:4000]


def _provider_summary(label: str, rows: list[AccountSnapshot]) -> str:
    healthy = sum(row.status == "ok" for row in rows)
    remaining = [window.remaining_percent for row in rows for window in row.windows if window.remaining_percent is not None]
    best = max(remaining) if remaining else None
    best_text = f"best {round(best)}% left" if best is not None else "usage unavailable"
    return f"**{label}** · {healthy}/{len(rows)} healthy · {best_text}"


def render_summary(openai: Iterable[AccountSnapshot], claude: Iterable[AccountSnapshot]) -> str:
    openai_rows, claude_rows = list(openai), list(claude)
    return "\n".join([
        "# Station · Account capacity",
        "",
        _provider_summary("OpenAI", openai_rows),
        _provider_summary("Claude Code", claude_rows),
        "",
        "Detailed per-account panels refresh automatically.",
    ])


def _window_identity_label(window: UsageWindow) -> str:
    return str(window.label or "")[:256]


def _alert_slot_key(provider: str, account: AccountSnapshot, window: UsageWindow) -> str:
    payload = "\x1f".join((
        provider,
        account.credential_id,
        _window_identity_label(window),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _alert_key(provider: str, account: AccountSnapshot, window: UsageWindow) -> str:
    payload = "\x1f".join((
        provider,
        account.credential_id,
        _window_identity_label(window),
        str(window.reset_at or ""),
    ))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_credential_id(value: object) -> str:
    return _public_credential_id(value)


def collect_threshold_alerts(
    providers: Iterable[tuple[str, str, Iterable[AccountSnapshot]]],
    threshold: float,
    already_alerted: set[str],
) -> tuple[list[QuotaAlert], set[str]]:
    alerts: list[QuotaAlert] = []
    active: set[str] = set(already_alerted)
    bounded_threshold = max(1.0, min(100.0, float(threshold)))
    for provider, provider_label, accounts in providers:
        for account in accounts:
            for window in account.windows:
                if window.remaining_percent is None:
                    continue
                slot = _alert_slot_key(provider, account, window)
                fingerprint = _alert_key(provider, account, window)
                entry = f"{slot}:{fingerprint}"
                prior_slot_entries = {
                    value for value in active
                    if len(value) == 129 and value.startswith(f"{slot}:")
                }
                active.difference_update(prior_slot_entries)
                active.discard(fingerprint)
                remaining = max(0.0, min(100.0, float(window.remaining_percent)))
                used = 100.0 - remaining
                if used < bounded_threshold:
                    continue
                active.add(entry)
                if entry in already_alerted or fingerprint in already_alerted:
                    continue
                alerts.append(QuotaAlert(
                    key=entry,
                    provider_label=provider_label,
                    credential_id=_safe_credential_id(account.credential_id),
                    owner_name=account.owner_name[:64],
                    window_label=_safe_usage_label(window.label),
                    used_percent=used,
                    reset_at=window.reset_at,
                ))
    return alerts, active


def render_quota_alert(
    alerts: Iterable[QuotaAlert], *, account_control_channel_id: int
) -> str:
    rows = list(alerts)
    header = "## Station · Quota alert"
    footer = (
        "\n\n"
        f"Prepare a switch in <#{int(account_control_channel_id)}> or ask Operator here."
    )
    rendered_rows: list[str] = []
    for alert in rows[:12]:
        owner = alert.owner_name or "Unassigned"
        reset = _reset_text(alert.reset_at)
        row = (
            f"**{alert.provider_label} · {owner}** · `{alert.credential_id[:16]}` · "
            f"{alert.window_label} · **{round(alert.used_percent)}% used**{reset}"
        )
        omitted_after_add = len(rows) - len(rendered_rows) - 1
        suffix_reserve = (
            len(f"\n… {omitted_after_add} more threshold crossings.")
            if omitted_after_add > 0 else 0
        )
        candidate = "\n".join([header, "", *rendered_rows, row]) + footer
        if len(candidate) + suffix_reserve > 1900:
            break
        rendered_rows.append(row)
    omitted = len(rows) - len(rendered_rows)
    lines = [header, "", *rendered_rows]
    if omitted:
        lines.append(f"… {omitted} more threshold crossings.")
    return "\n".join(lines) + footer


def collect_provider_snapshots(provider: str, aliases: dict[str, str] | None = None) -> list[AccountSnapshot]:
    """Fetch current per-credential usage without exposing token/label metadata."""
    from agent.account_usage import fetch_account_usage
    from agent.credential_pool import load_pool

    snapshots: list[AccountSnapshot] = []
    pool = load_pool(provider)
    for index, entry in enumerate(pool.entries(), start=1):
        if (
            provider == "anthropic"
            and hasattr(pool, "_entry_needs_refresh")
            and hasattr(pool, "_refresh_entry")
        ):
            try:
                if pool._entry_needs_refresh(entry):
                    entry = pool._refresh_entry(entry, force=False) or entry
            except Exception as exc:  # noqa: BLE001 - private provider refresh has no stable error base.
                logger.warning("Claude credential refresh failed safely: %s", type(exc).__name__)
        raw_status = str(getattr(entry, "last_status", None) or "").lower()
        status = raw_status if raw_status in {"ok", "exhausted", "dead"} else "unknown"
        token = getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", None)
        windows: list[UsageWindow] = []
        if token:
            try:
                usage = fetch_account_usage(
                    provider,
                    base_url=getattr(entry, "base_url", None),
                    api_key=token,
                )
                for window in (getattr(usage, "windows", ()) or ()):
                    used = getattr(window, "used_percent", None)
                    remaining = None if used is None else max(0.0, min(100.0, 100.0 - float(used)))
                    reset_at = getattr(window, "reset_at", None)
                    windows.append(UsageWindow(
                        str(getattr(window, "label", "Limit") or "Limit"),
                        remaining,
                        reset_at.isoformat() if reset_at else None,
                    ))
            except Exception as exc:  # noqa: BLE001 - provider clients expose no stable error base.
                logger.warning("Usage fetch failed safely for %s: %s", provider, type(exc).__name__)
                windows = []
        observed_remaining = [
            window.remaining_percent
            for window in windows
            if window.remaining_percent is not None
        ]
        if observed_remaining:
            status = "ok" if any(value > 0.0 for value in observed_remaining) else "exhausted"
        credential_id = str(getattr(entry, "id", "") or "unknown")[:64]
        snapshots.append(AccountSnapshot(
            index,
            credential_id,
            status,
            tuple(windows),
            str((aliases or {}).get(credential_id) or ""),
        ))
    return snapshots


def default_store() -> MessageStateStore:
    return MessageStateStore(Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes"))


class DiscordAccountUsageMonitor:
    """Edit one compact message per provider on a bounded cadence."""

    def __init__(
        self,
        client,
        config: MonitorConfig,
        store: MessageStateStore | None = None,
        *,
        alert_store: QuotaAlertStateStore | None = None,
    ):
        self.client = client
        self.config = config
        self.store = store or default_store()
        home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
        self.alert_store = alert_store or QuotaAlertStateStore(home)
        self._alert_lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self.config.enabled and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run(), name="agk-account-usage-monitor")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def _channel(self, channel_id: int):
        if not channel_id:
            return None
        channel = self.client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(channel_id)
            except Exception as exc:  # noqa: BLE001 - Discord client errors vary by adapter/version.
                logger.warning("Could not fetch monitor channel safely: %s", type(exc).__name__)
                return None
        return channel

    async def _sync_voice_channels(
        self,
        category,
        provider: str,
        provider_label: str,
        rows: list[AccountSnapshot],
        state: dict[str, int],
        *,
        seed_channel=None,
    ) -> None:
        if category is None or getattr(category, "channels", None) is None:
            return
        guild = getattr(category, "guild", None)
        channels = list(getattr(category, "channels", ()) or ())
        by_id = {int(channel.id): channel for channel in channels if getattr(channel, "id", None)}
        used_ids: set[int] = set()
        for account in rows:
            legacy_key = f"voice:{provider}:{account.credential_id}"
            has_owner = bool(account.owner_name.strip())
            key = voice_binding_key(provider, account.owner_name) if has_owner else legacy_key
            desired = voice_channel_name(provider_label, account)
            channel = by_id.get(state.get(key, 0))
            if channel is None and has_owner:
                channel = by_id.get(state.get(legacy_key, 0))
            if channel is not None and int(channel.id) in used_ids:
                channel = None
            if channel is None:
                owner_prefix = f"{account.owner_name}-".casefold() if account.owner_name else ""
                channel = next(
                    (
                        candidate for candidate in channels
                        if int(getattr(candidate, "id", 0) or 0) not in used_ids
                        and owner_prefix
                        and str(getattr(candidate, "name", "")).casefold().startswith(owner_prefix)
                        and f"-{provider_label}".casefold() in str(getattr(candidate, "name", "")).casefold()
                    ),
                    None,
                )
            if channel is None and seed_channel is not None and int(getattr(seed_channel, "id", 0) or 0) not in used_ids:
                seed_name = str(getattr(seed_channel, "name", "")).casefold()
                if account.owner_name and seed_name.startswith(f"{account.owner_name}-".casefold()):
                    channel = seed_channel
            if channel is None and guild is not None and hasattr(guild, "create_voice_channel"):
                try:
                    channel = await guild.create_voice_channel(
                        desired,
                        category=category,
                        reason="AGK Station per-account quota monitor",
                    )
                    channels.append(channel)
                    by_id[int(channel.id)] = channel
                except Exception as exc:  # noqa: BLE001 - Discord client errors vary by adapter/version.
                    logger.warning("Could not create quota voice channel safely: %s", type(exc).__name__)
                    continue
            if channel is None:
                continue
            used_ids.add(int(channel.id))
            state[key] = int(channel.id)
            if has_owner:
                state.pop(legacy_key, None)
            if str(getattr(channel, "name", "")) != desired and hasattr(channel, "edit"):
                try:
                    await channel.edit(name=desired, reason="AGK Station quota refresh")
                except Exception as exc:  # noqa: BLE001 - Discord client errors vary by adapter/version.
                    logger.warning("Could not rename quota voice channel safely: %s", type(exc).__name__)


    async def _sync_active_channel(
        self,
        category,
        openai_rows: list[AccountSnapshot],
        claude_rows: list[AccountSnapshot],
        state: dict[str, int],
        aliases: dict[str, dict[str, str]],
    ) -> None:
        """Keep exactly one ACTIVE channel for the Hermes session model+account."""
        if category is None or getattr(category, "channels", None) is None:
            return
        guild = getattr(category, "guild", None)
        provider, model = load_hermes_active_model()
        pool_key = provider_pool_key(provider)
        rows: list[AccountSnapshot] = []
        provider_aliases: dict[str, str] = {}
        if pool_key == "anthropic":
            rows = claude_rows
            provider_aliases = aliases.get("anthropic", {}) or {}
        elif pool_key == "openai-codex":
            rows = openai_rows
            provider_aliases = aliases.get("openai-codex", {}) or {}
        elif pool_key:
            provider_aliases = aliases.get(pool_key, {}) or {}
            try:
                rows = collect_provider_snapshots(pool_key, provider_aliases)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Active snapshot failed safely for %s: %s", pool_key, type(exc).__name__)
                rows = []
        active = resolve_active_account(pool_key, rows, provider_aliases) if pool_key else None
        owner = (active.owner_name if active else None) or ("n/a" if not pool_key else "unknown")
        desired = hermes_active_channel_name(provider, model, owner)
        key = "active-voice:hermes"
        channels = list(getattr(category, "channels", ()) or ())
        by_id = {int(channel.id): channel for channel in channels if getattr(channel, "id", None)}
        channel = by_id.get(int(state.get(key, 0) or 0))
        # Also reclaim legacy dual ACTIVE-* channels into the single indicator.
        legacy = [
            candidate for candidate in channels
            if str(getattr(candidate, "name", "")).casefold().startswith("active")
        ]
        if channel is None and legacy:
            channel = legacy[0]
        if channel is None and guild is not None and hasattr(guild, "create_voice_channel"):
            try:
                channel = await guild.create_voice_channel(
                    desired,
                    category=category,
                    reason="AGK Station Hermes active model/account",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not create Hermes active channel safely: %s", type(exc).__name__)
                return
        if channel is None:
            return
        state[key] = int(channel.id)
        # Drop legacy dual keys / rename extras away
        state.pop("active-voice:anthropic", None)
        state.pop("active-voice:openai-codex", None)
        if str(getattr(channel, "name", "")) != desired and hasattr(channel, "edit"):
            try:
                await channel.edit(name=desired, reason="AGK Station Hermes active model refresh")
            except Exception as exc:  # noqa: BLE001
                logger.warning("Could not rename Hermes active channel safely: %s", type(exc).__name__)
        # Delete surplus legacy ACTIVE channels (keep the one we edited)
        for extra in legacy:
            if int(getattr(extra, "id", 0) or 0) == int(channel.id):
                continue
            if hasattr(extra, "delete"):
                try:
                    await extra.delete(reason="AGK Station: one Hermes active model channel only")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Could not delete legacy ACTIVE channel safely: %s", type(exc).__name__)

    async def _publish_quota_alerts(
        self,
        openai_rows: list[AccountSnapshot],
        claude_rows: list[AccountSnapshot],
    ) -> None:
        async with self._alert_lock:
            async with self.alert_store.locked():
                previous = self.alert_store.load()
                alerts, active = collect_threshold_alerts(
                    (
                        ("openai-codex", "OpenAI", openai_rows),
                        ("anthropic", "Claude", claude_rows),
                    ),
                    self.config.alert_used_threshold_percent,
                    previous,
                )
                if not alerts:
                    self.alert_store.save(active)
                    return
                channel = await self._channel(self.config.alert_channel_id)
                if channel is None or not hasattr(channel, "send"):
                    self.alert_store.save(previous & active)
                    return
                await channel.send(
                    render_quota_alert(alerts, account_control_channel_id=1542563923809796140)
                )
                self.alert_store.save(active)

    async def refresh_once(self) -> None:
        aliases = load_owner_aliases(Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes"))
        openai_rows, claude_rows = await asyncio.wait_for(
            asyncio.gather(
                asyncio.to_thread(collect_provider_snapshots, "openai-codex", aliases.get("openai-codex", {})),
                asyncio.to_thread(collect_provider_snapshots, "anthropic", aliases.get("anthropic", {})),
            ),
            timeout=60,
        )
        root = await self._channel(self.config.summary_or_category_id)
        openai_channel = await self._channel(self.config.openai_channel_id)
        state = self.store.load()
        category = root if getattr(root, "channels", None) is not None else getattr(openai_channel, "category", None)
        await self._sync_voice_channels(
            category, "openai-codex", "OpenAI", openai_rows, state, seed_channel=openai_channel
        )
        await self._sync_voice_channels(category, "anthropic", "Claude", claude_rows, state)
        await self._sync_active_channel(
            category, openai_rows, claude_rows, state, aliases
        )
        self.store.save(state)
        await self._publish_quota_alerts(openai_rows, claude_rows)

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - monitor loop is an intentional fail-safe boundary.
                logger.warning("Station account usage refresh failed safely: %s", type(exc).__name__)
            await asyncio.sleep(self.config.interval_seconds)
