"""Lightweight redacted OpenAI/Claude quota panels for Discord."""
from __future__ import annotations

import json
import math
import os
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


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
class MonitorConfig:
    summary_or_category_id: int
    openai_channel_id: int
    claude_channel_id: int = 0
    interval_seconds: int = 300
    claude_channel_name: str = "claudecode-all-accounts"

    @classmethod
    def from_extra(cls, extra: dict | None) -> "MonitorConfig":
        values = extra or {}
        interval = max(180, min(3600, int(values.get("usage_monitor_interval_seconds", 300) or 300)))
        return cls(
            summary_or_category_id=int(values.get("usage_monitor_channel_id", 0) or 0),
            openai_channel_id=int(values.get("usage_monitor_openai_channel_id", 0) or 0),
            claude_channel_id=int(values.get("usage_monitor_claude_channel_id", 0) or 0),
            interval_seconds=interval,
            claude_channel_name=str(values.get("usage_monitor_claude_channel_name", "claudecode-all-accounts") or "claudecode-all-accounts")[:100],
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
        return {
            str(k): int(v)
            for k, v in raw.items()
            if (k in {"summary", "openai", "claude"} or str(k).startswith("voice:"))
            and isinstance(v, int)
        }

    def save(self, values: dict[str, int]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        clean = {
            str(k): int(v)
            for k, v in values.items()
            if (k in {"summary", "openai", "claude"} or str(k).startswith("voice:"))
            and isinstance(v, int)
        }
        temporary = self.path.with_name(".discord_usage_monitor.json.new")
        temporary.write_text(json.dumps(clean, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)


def remaining_bar(value: float | int | None) -> str:
    if value is None:
        return "░░░░░░░░░░"
    remaining = max(0.0, min(100.0, float(value)))
    filled = max(0, min(10, round(remaining / 10.0))) if remaining else 0
    return "█" * filled + "░" * (10 - filled)


def load_owner_aliases(hermes_home: Path) -> dict[str, dict[str, str]]:
    """Load only stable credential-id → owner-name mappings from the redacted registry."""
    path = Path(hermes_home) / "provider-account-aliases.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    result: dict[str, dict[str, str]] = {}
    providers = payload.get("providers") if isinstance(payload, dict) else {}
    for provider, rows in (providers or {}).items():
        mapped: dict[str, str] = {}
        for row in rows if isinstance(rows, list) else ():
            if not isinstance(row, dict):
                continue
            credential_id = str(row.get("credential_id") or "").strip()
            owner_name = str(row.get("owner_nickname") or "").strip()
            if credential_id and owner_name:
                mapped[credential_id] = owner_name[:64]
        if mapped:
            result[str(provider)] = mapped
    return result


def voice_channel_name(provider_label: str, account: AccountSnapshot) -> str:
    owner = account.owner_name.strip() or f"Account-{account.credential_id[:8]}"
    owner = "".join(ch for ch in owner if ch.isalnum() or ch in {"-", "_"}).strip("-_") or "Account"
    remaining = next(
        (window.remaining_percent for window in account.windows if window.remaining_percent is not None),
        None,
    )
    used = None if remaining is None else 100.0 - max(0.0, min(100.0, float(remaining)))
    percent = "?" if used is None else str(round(used))
    return f"{owner}-{provider_label} : {percent}%"[:100]


def _reset_text(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return f" · resets <t:{int(parsed.timestamp())}:R>"
    except (TypeError, ValueError):
        return ""


def render_provider_panel(provider_label: str, accounts: Iterable[AccountSnapshot]) -> str:
    rows = list(accounts)
    lines = [f"## {provider_label} · all accounts", ""]
    if not rows:
        return "\n".join(lines + ["No account connected."])
    for account in rows:
        status = account.status if account.status in {"ok", "exhausted", "dead"} else "unknown"
        lines.append(f"**Account {account.index} [`{account.credential_id}`]** · `{status}`")
        if not account.windows:
            lines.append("`░░░░░░░░░░` Usage unavailable")
        for window in account.windows[:3]:
            remaining = window.remaining_percent
            if remaining is None:
                lines.append(f"`{remaining_bar(None)}` {window.label} · unavailable")
            else:
                remaining = max(0.0, min(100.0, float(remaining)))
                lines.append(
                    f"`{remaining_bar(remaining)}` {window.label} · {round(remaining)}% remaining{_reset_text(window.reset_at)}"
                )
        lines.append("")
    lines.append("Auto-refresh · lightweight · no credentials displayed")
    return "\n".join(lines)[:4000]


def _provider_summary(label: str, rows: list[AccountSnapshot]) -> str:
    healthy = sum(row.status == "ok" for row in rows)
    remaining = [window.remaining_percent for row in rows for window in row.windows if window.remaining_percent is not None]
    best = max(remaining) if remaining else None
    best_text = f"best {round(best)}%" if best is not None else "usage unavailable"
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
            except Exception:
                pass
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
            except Exception:
                windows = []
        if windows and status == "unknown":
            status = "ok"
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

    def __init__(self, client, config: MonitorConfig, store: MessageStateStore | None = None):
        self.client = client
        self.config = config
        self.store = store or default_store()
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
            except Exception:
                return None
        return channel

    async def _find_or_create_claude_channel(self, root, openai_channel):
        if self.config.claude_channel_id:
            return await self._channel(self.config.claude_channel_id)
        category = root if getattr(root, "channels", None) is not None else getattr(openai_channel, "category", None)
        if category is None:
            return root if hasattr(root, "send") else None
        for channel in getattr(category, "channels", ()):
            if str(getattr(channel, "name", "")).casefold() == self.config.claude_channel_name.casefold():
                return channel
        guild = getattr(category, "guild", None)
        if guild is not None and hasattr(guild, "create_text_channel"):
            try:
                return await guild.create_text_channel(
                    self.config.claude_channel_name,
                    category=category,
                    reason="AGK Station account capacity monitor",
                )
            except Exception:
                return root if hasattr(root, "send") else None
        return None

    async def _summary_channel(self, root, openai_channel):
        if hasattr(root, "send"):
            return root
        category = root if getattr(root, "channels", None) is not None else getattr(openai_channel, "category", None)
        if category is not None:
            for channel in getattr(category, "channels", ()):
                if str(getattr(channel, "name", "")).casefold() == "station-account-capacity":
                    return channel
            guild = getattr(category, "guild", None)
            if guild is not None and hasattr(guild, "create_text_channel"):
                try:
                    return await guild.create_text_channel(
                        "station-account-capacity",
                        category=category,
                        reason="AGK Station account capacity monitor",
                    )
                except Exception:
                    pass
        return openai_channel

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
            key = f"voice:{provider}:{account.credential_id}"
            desired = voice_channel_name(provider_label, account)
            channel = by_id.get(state.get(key, 0))
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
                except Exception as exc:
                    logger.warning("Could not create quota voice channel safely: %s", type(exc).__name__)
                    continue
            if channel is None:
                continue
            used_ids.add(int(channel.id))
            state[key] = int(channel.id)
            if str(getattr(channel, "name", "")) != desired and hasattr(channel, "edit"):
                try:
                    await channel.edit(name=desired, reason="AGK Station quota refresh")
                except Exception as exc:
                    logger.warning("Could not rename quota voice channel safely: %s", type(exc).__name__)

    async def _upsert(self, key: str, channel, title: str, description: str, state: dict[str, int]) -> None:
        if channel is None or not hasattr(channel, "send"):
            return
        try:
            import discord
            embed = discord.Embed(title=title, description=description[:4096], color=discord.Color.from_rgb(17, 17, 17))
            embed.set_footer(text="Station · refreshes every 5 minutes · no secrets")
        except Exception:
            embed = None
        message = None
        message_id = state.get(key)
        if message_id and hasattr(channel, "fetch_message"):
            try:
                message = await channel.fetch_message(message_id)
            except Exception:
                message = None
        if message is not None:
            await message.edit(content=None if embed else description[:1900], embed=embed)
        else:
            message = await channel.send(content=None if embed else description[:1900], embed=embed)
            state[key] = int(message.id)

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
        claude_channel = await self._find_or_create_claude_channel(root, openai_channel)
        summary_channel = await self._summary_channel(root, openai_channel)
        state = self.store.load()
        category = root if getattr(root, "channels", None) is not None else getattr(openai_channel, "category", None)
        await self._sync_voice_channels(
            category, "openai-codex", "OpenAI", openai_rows, state, seed_channel=openai_channel
        )
        await self._sync_voice_channels(category, "anthropic", "Claude", claude_rows, state)
        await self._upsert("summary", summary_channel, "Station · Account capacity", render_summary(openai_rows, claude_rows), state)
        await self._upsert("openai", openai_channel, "OpenAI Codex · all accounts", render_provider_panel("OpenAI Codex", openai_rows), state)
        await self._upsert("claude", claude_channel, "Claude Code · all accounts", render_provider_panel("Claude Code", claude_rows), state)
        self.store.save(state)

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Station account usage refresh failed safely: %s", type(exc).__name__)
            await asyncio.sleep(self.config.interval_seconds)
