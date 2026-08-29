"""Private persistent Discord account control center for the AGK Station."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

try:
    import discord
except ImportError:  # pragma: no cover - source-contract tests run without discord.py
    discord = None

def _load_roster_api():
    try:
        from .agk_account_control import load_account_roster, render_account_roster
    except ImportError:  # pragma: no cover - direct-file test loading
        from agk_account_control import load_account_roster, render_account_roster
    return load_account_roster, render_account_roster

ACCOUNT_CONTROL_GUILD_ID = 1541131439599386644
ACCOUNT_CONTROL_CATEGORY_ID = 1542505218569150585
ACCOUNT_CONTROL_CHANNEL_ID = 1542563923809796140
ACCOUNT_CONTROL_MESSAGE_ID = 1542563946135814278
ACCOUNT_CONTROL_OWNER_ID = 1441423462492016821
ACCOUNT_CONTROL_CHANNEL_NAME = "account-control"
_DISCORD_CONTENT_LIMIT = 2000
_SAFE_DEVICE_CODE = re.compile(r"[A-Za-z0-9-]{1,128}\Z")
_SAFE_OAUTH_QUERY_KEYS = {
    "client_id", "response_type", "redirect_uri", "scope",
    "code_challenge", "code_challenge_method",
}
_OAUTH_URL_RULES = {
    "openai-codex": {
        "auth.openai.com": {"/codex/device", "/oauth/authorize"},
    },
    "anthropic": {
        "claude.ai": {"/oauth/authorize"},
        "console.anthropic.com": {"/oauth/authorize"},
    },
}


@dataclass(frozen=True)
class AccountControlState:
    channel_id: int
    message_id: int


@dataclass(frozen=True)
class _FallbackSnowflake:
    id: int


def _hermes_home(adapter: Any) -> Path:
    configured = getattr(adapter, "hermes_home", None)
    if configured is not None:
        return Path(configured)
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def _state_path(adapter: Any) -> Path:
    return _hermes_home(adapter) / "account_control_state.json"


def _read_state(adapter: Any) -> AccountControlState | None:
    path = _state_path(adapter)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            payload = json.load(handle)
        return AccountControlState(
            channel_id=int(payload["channel_id"]),
            message_id=int(payload["message_id"]),
        )
    except (KeyError, TypeError, ValueError, OSError):
        return None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_state(adapter: Any, state: AccountControlState) -> None:
    path = _state_path(adapter)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".new", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(asdict(state), handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _permission(**values):
    return discord.PermissionOverwrite(**values) if discord else SimpleNamespace(**values)


def _snowflake(snowflake_id: int):
    """Build an exact Discord overwrite target without relying on member cache."""
    return discord.Object(id=int(snowflake_id)) if discord else _FallbackSnowflake(int(snowflake_id))


def _load_records(adapter: Any):
    loader = getattr(adapter, "load_account_roster", None)
    if callable(loader):
        return tuple(loader())
    load, _renderer = _load_roster_api()
    return tuple(load(_hermes_home(adapter)))


def _reset_timestamp(value: Any) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return f"<t:{int(parsed.timestamp())}:R>"
    except (TypeError, ValueError):
        return "unknown reset"


def _render_records(records: Any, *, selected_provider: str = "") -> str:
    rows = tuple(records or ())
    if rows:
        labels = {"openai-codex": "OpenAI", "anthropic": "Claude"}
        if selected_provider in labels:
            selected = [row for row in rows if getattr(row, "provider", "") == selected_provider]
            lines = [f"## {labels[selected_provider]} accounts", ""]
            for record in selected[:25]:
                owner = str(getattr(record, "owner_name", "") or "Unassigned")[:64]
                credential = str(getattr(record, "credential_id", "") or "unknown")[:16]
                status = str(getattr(record, "status", "") or "unknown")
                priority = int(getattr(record, "priority", 0) or 0)
                lines.append(f"**{owner}** · `{credential}` · `{status}` · P{priority}")
                for window in tuple(getattr(record, "windows", ()) or ())[:2]:
                    remaining = getattr(window, "remaining_percent", None)
                    usage = "usage unavailable" if remaining is None else f"{round(100.0 - float(remaining))}% used"
                    lines.append(
                        f"- {str(getattr(window, 'label', '') or 'Limit')[:40]} · {usage} · "
                        f"{_reset_timestamp(getattr(window, 'reset_at', None))}"
                    )
                lines.append("")
            lines.append("Choose an account, then run one action.")
            return "\n".join(lines)[:_DISCORD_CONTENT_LIMIT]
        summary = ["## Station · Accounts", ""]
        for provider, label in labels.items():
            provider_rows = [row for row in rows if getattr(row, "provider", "") == provider]
            summary.append(f"**{label}** · {len(provider_rows)} account{'s' if len(provider_rows) != 1 else ''}")
        summary.extend(["", "Choose a provider to view its accounts."])
        return "\n".join(summary)
    try:
        _load, renderer = _load_roster_api()
    except ImportError:
        return "# Station · Account roster\n\nNo connected accounts."
    rendered = renderer(records)
    if len(rendered) <= _DISCORD_CONTENT_LIMIT:
        return rendered
    suffix = "\n\n… roster truncated; use the account selector or canonical CLI for remaining rows."
    return rendered[: _DISCORD_CONTENT_LIMIT - len(suffix)] + suffix


def _render(adapter: Any) -> str:
    return _render_records(_load_records(adapter))


class _PrePoolSnapshotStore:
    """Private durable pre-OAuth pool snapshots consumed by Task 3."""

    def __init__(self, hermes_home: Path):
        self.hermes_home = Path(hermes_home)
        self.directory = self.hermes_home / "state/account-oauth"

    def pool_ids(self, provider: str) -> tuple[str, ...]:
        from agent.credential_pool import load_pool
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        token = set_hermes_home_override(self.hermes_home)
        try:
            return tuple(str(entry.id) for entry in load_pool(provider).entries())
        finally:
            reset_hermes_home_override(token)

    def capture(self, attempt_id: str, provider: str, credential_ids: Any) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", str(attempt_id)):
            raise ValueError("invalid OAuth attempt ID")
        values = [str(value) for value in credential_ids]
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        path = self.directory / f"{attempt_id}.pre-pool.json"
        descriptor, name = tempfile.mkstemp(prefix=".pre-pool.", suffix=".new", dir=self.directory)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "provider": provider, "credential_ids": values}, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, attempt: Any) -> tuple[str, ...]:
        path = self.directory / f"{attempt.attempt_id}.pre-pool.json"
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            metadata = os.fstat(descriptor)
            if metadata.st_size > 32_768:
                raise ValueError("invalid pre-pool snapshot")
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                descriptor = -1
                payload = json.load(handle)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if payload.get("version") != 1 or payload.get("provider") != attempt.provider:
            raise ValueError("pre-pool snapshot does not match attempt")
        rows = payload.get("credential_ids")
        if not isinstance(rows, list):
            raise ValueError("invalid pre-pool snapshot")
        return tuple(str(value) for value in rows)


def _probe_candidate(provider: str, entry: Any) -> bool:
    """Verify the exact candidate through the canonical provider usage seam."""
    from agent.account_usage import fetch_account_usage

    token = getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", None)
    if not token:
        return False
    try:
        return fetch_account_usage(
            provider,
            base_url=getattr(entry, "base_url", None),
            api_key=token,
        ) is not None
    except Exception:  # noqa: BLE001 - provider clients expose no common error base
        return False


def _build_account_control_services(adapter: Any) -> Any:
    """Construct the approved Task 2 runner and Task 3 coordinator for production."""
    try:
        from .agk_account_oauth import OAuthAttemptStore, OAuthRunner
        from .agk_account_transactions import AccountTransactionCoordinator
        from .agk_account_control import AliasRegistry
    except ImportError:  # pragma: no cover - direct-file loading
        from agk_account_oauth import OAuthAttemptStore, OAuthRunner
        from agk_account_transactions import AccountTransactionCoordinator
        from agk_account_control import AliasRegistry

    home = _hermes_home(adapter)
    attempt_store = OAuthAttemptStore(home)
    runner = OAuthRunner(attempt_store)
    snapshot_store = _PrePoolSnapshotStore(home)
    coordinator = AccountTransactionCoordinator(
        home,
        attempt_store,
        AliasRegistry(home / "provider-account-aliases.json"),
        pre_pool_ids=snapshot_store.read,
        probe_candidate=_probe_candidate,
        refresh_surfaces=lambda: None,
    )
    return SimpleNamespace(
        runner=runner, coordinator=coordinator, snapshot_store=snapshot_store
    )


_ViewBase = discord.ui.View if discord else object


class AccountControlView(_ViewBase):
    """Persistent owner-only controls for canonical account operations."""

    def __init__(
        self,
        adapter: Any,
        *,
        runner: Any = None,
        coordinator: Any = None,
        channel_id: int = ACCOUNT_CONTROL_CHANNEL_ID,
    ):
        if discord:
            super().__init__(timeout=None)
        else:
            super().__init__()
            self.timeout = None
        self.adapter = adapter
        self.runner = runner or getattr(adapter, "_account_control_oauth_runner", None)
        self.coordinator = coordinator or getattr(adapter, "_account_control_coordinator", None)
        self.channel_id = int(channel_id)
        self.selected_provider = ""
        self.selected_credential_id = ""
        self.selected_attempt_id = ""
        self.selected_owner_name = ""
        self.records: tuple[Any, ...] = ()
        self.message = None
        if discord:
            self._build_components()

    def _build_components(self) -> None:
        self.clear_items()
        provider = discord.ui.Select(
            placeholder="Choose OpenAI or Claude",
            options=[
                discord.SelectOption(label="OpenAI / ChatGPT", value="openai-codex"),
                discord.SelectOption(label="Anthropic / Claude", value="anthropic"),
            ],
            custom_id="agkacct:provider",
            row=0,
        )
        provider.callback = self.dispatch
        self.add_item(provider)
        matching = [
            record
            for record in self.records
            if getattr(record, "provider", None) == self.selected_provider
        ][:25]
        account_options = [
            discord.SelectOption(
                label=(
                    f"{getattr(record, 'owner_name', '') or 'Unassigned'} · "
                    f"{getattr(record, 'credential_id', 'unknown')}"
                )[:100],
                value=str(getattr(record, "credential_id", ""))[:100],
                description=f"Status: {getattr(record, 'status', 'unknown')}"[:100],
            )
            for record in matching
            if getattr(record, "credential_id", None)
        ]
        account = discord.ui.Select(
            placeholder="Choose an account",
            options=account_options or [
                discord.SelectOption(label="No account available", value="none")
            ],
            custom_id="agkacct:account",
            disabled=not account_options,
            row=1,
        )
        account.callback = self.dispatch
        self.add_item(account)
        for label, custom_id, style, row in (
            ("Switch", "agkacct:switch", discord.ButtonStyle.primary, 2),
            ("Add account", "agkacct:add", discord.ButtonStyle.success, 2),
            ("Reconnect", "agkacct:reconnect", discord.ButtonStyle.secondary, 2),
            ("Refresh", "agkacct:refresh", discord.ButtonStyle.secondary, 3),
            ("Close session", "agkacct:close", discord.ButtonStyle.danger, 3),
        ):
            button = discord.ui.Button(label=label, style=style, custom_id=custom_id, row=row)
            button.callback = self.dispatch
            self.add_item(button)

    async def _authorized(self, interaction: Any) -> bool:
        allowed = (
            int(getattr(getattr(interaction, "user", None), "id", 0)) == ACCOUNT_CONTROL_OWNER_ID
            and int(getattr(interaction, "guild_id", 0) or 0) == ACCOUNT_CONTROL_GUILD_ID
            and int(getattr(interaction, "channel_id", 0) or 0) == self.channel_id
        )
        if allowed:
            return True
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "This private account control action is not authorized.", ephemeral=True
            )
        return False

    async def dispatch(self, interaction: Any) -> None:
        """Route every stable component ID through the same fail-closed gate."""
        if not await self._authorized(interaction):
            return
        try:
            await self._dispatch_authorized(interaction)
        except Exception as exc:  # noqa: BLE001 - action seams expose no common error base
            await self._safe_failure(interaction, exc)

    async def _safe_failure(self, interaction: Any, exc: Exception) -> None:
        text = f"Account action failed safely ({type(exc).__name__}). Try Refresh or retry."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            return

    async def _dispatch_authorized(self, interaction: Any) -> None:
        custom_id = str((getattr(interaction, "data", None) or {}).get("custom_id") or "")
        values = (getattr(interaction, "data", None) or {}).get("values") or []
        if custom_id == "agkacct:provider":
            await self._select_provider(interaction, values)
        elif custom_id == "agkacct:account":
            await self._select_account(interaction, values)
        elif custom_id == "agkacct:switch":
            await self._switch(interaction)
        elif custom_id == "agkacct:refresh":
            await interaction.response.defer(ephemeral=True)
            outcome = await self.refresh_message()
            await interaction.followup.send(outcome, ephemeral=True)
        elif custom_id == "agkacct:add":
            if self.selected_provider not in {"openai-codex", "anthropic"}:
                await interaction.response.send_message("Select a provider first.", ephemeral=True)
            elif discord:
                await interaction.response.send_modal(OwnerNicknameModal(self))
            else:
                await interaction.response.send_message(
                    "Submit an owner nickname to start OAuth.", ephemeral=True
                )
        elif custom_id == "agkacct:reconnect":
            await self._request_reconnect(interaction)
        elif custom_id == "agkacct:confirm-reconnect":
            await self.start_reconnect(interaction)
        elif custom_id == "agkacct:claude-code":
            if self.selected_provider != "anthropic" or not self.selected_attempt_id:
                await interaction.response.send_message(
                    "No active Claude OAuth attempt is selected.", ephemeral=True
                )
            elif discord:
                await interaction.response.send_modal(
                    ClaudeCodeModal(
                        self, self.selected_provider, self.selected_attempt_id
                    )
                )
            else:
                await interaction.response.send_message(
                    "Submit the one-time Claude code.", ephemeral=True
                )
        elif custom_id == "agkacct:close":
            await self.close_attempt(interaction)
        else:
            await interaction.response.send_message("Unknown account action.", ephemeral=True)

    async def _select_provider(self, interaction: Any, values: list[Any]) -> None:
        provider = str(values[0]) if values else ""
        if provider not in {"openai-codex", "anthropic"}:
            await interaction.response.send_message("Choose a canonical provider.", ephemeral=True)
            return
        self.selected_provider = provider
        self.selected_credential_id = ""
        self.selected_owner_name = ""
        await interaction.response.defer()
        self.records = await asyncio.to_thread(_load_records, self.adapter)
        if discord:
            self._build_components()
        await interaction.edit_original_response(
            content=_render_records(self.records, selected_provider=self.selected_provider),
            view=self,
        )

    async def _select_account(self, interaction: Any, values: list[Any]) -> None:
        credential_id = str(values[0]) if values else ""
        if not self.selected_provider or not credential_id or len(credential_id) > 128:
            await interaction.response.send_message("Choose a valid account.", ephemeral=True)
            return
        selected = next(
            (
                record
                for record in self.records
                if getattr(record, "provider", None) == self.selected_provider
                and str(getattr(record, "credential_id", "")) == credential_id
            ),
            None,
        )
        if selected is None:
            await interaction.response.send_message(
                "That account is no longer in the canonical pool. Refresh first.", ephemeral=True
            )
            return
        self.selected_credential_id = credential_id
        self.selected_owner_name = str(getattr(selected, "owner_name", "") or "")
        await interaction.response.edit_message(
            content=_render_records(self.records, selected_provider=self.selected_provider),
            view=self,
        )

    async def _switch(self, interaction: Any) -> None:
        if self.selected_provider not in {"openai-codex", "anthropic"} or not self.selected_credential_id:
            await interaction.response.send_message(
                "Choose a provider and account before switching.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        status = await self.adapter._prefer_account_credential(
            self.selected_provider, self.selected_credential_id
        )
        await self.refresh_message()
        if status == "missing":
            text = "That account no longer exists in the canonical pool."
        elif status == "unavailable":
            text = "That account is not eligible to become preferred."
        elif status == "saved":
            provider_rows = [
                row for row in self.records
                if getattr(row, "provider", None) == self.selected_provider
            ]
            verified = bool(provider_rows) and str(
                getattr(provider_rows[0], "credential_id", "")
            ) == self.selected_credential_id
            text = (
                "Preferred account updated. Automatic quota rotation remains enabled."
                if verified
                else "Preference was saved, but canonical readback did not confirm ordering."
            )
        else:
            text = "Account preference returned an unknown safe status; no success was claimed."
        await interaction.followup.send(text, ephemeral=True)

    def _ensure_runner(self) -> Any:
        if self.runner is not None:
            return self.runner
        try:
            from .agk_account_oauth import OAuthAttemptStore, OAuthRunner
        except ImportError:  # pragma: no cover - direct-file loading
            from agk_account_oauth import OAuthAttemptStore, OAuthRunner
        self.runner = OAuthRunner(OAuthAttemptStore(_hermes_home(self.adapter)))
        self.adapter._account_control_oauth_runner = self.runner
        return self.runner

    async def start_add(self, interaction: Any, owner_name: str) -> None:
        if not await self._authorized(interaction):
            return
        await self._start_oauth(interaction, "add", str(owner_name).strip(), None)

    async def start_add_bound(
        self, interaction: Any, owner_name: str, provider: str
    ) -> None:
        if not await self._authorized(interaction):
            return
        await self._start_oauth(
            interaction,
            "add",
            str(owner_name).strip(),
            None,
            provider=provider,
        )

    async def _request_reconnect(self, interaction: Any) -> None:
        if (
            self.selected_provider not in {"openai-codex", "anthropic"}
            or not self.selected_credential_id
            or not getattr(self, "selected_owner_name", "")
        ):
            await interaction.response.send_message(
                "Select a named account before reconnecting.", ephemeral=True
            )
            return
        if discord:
            await interaction.response.send_message(
                "Reconnect replaces the selected credential only after verification.",
                view=ReconnectConfirmView(
                    self,
                    self.selected_provider,
                    self.selected_credential_id,
                    self.selected_owner_name,
                ),
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Confirm reconnect to replace the selected credential.", ephemeral=True
            )

    async def start_reconnect(self, interaction: Any) -> None:
        if not await self._authorized(interaction):
            return
        owner_name = str(getattr(self, "selected_owner_name", "") or "")
        if not self.selected_credential_id or not owner_name:
            await interaction.response.send_message(
                "The selected account is unavailable for reconnect.", ephemeral=True
            )
            return
        await self._start_oauth(
            interaction, "reconnect", owner_name, self.selected_credential_id
        )

    async def start_reconnect_bound(
        self,
        interaction: Any,
        provider: str,
        credential_id: str,
        owner_name: str,
    ) -> None:
        if not await self._authorized(interaction):
            return
        records = await asyncio.to_thread(_load_records, self.adapter)
        exact = next(
            (
                row for row in records
                if getattr(row, "provider", None) == provider
                and str(getattr(row, "credential_id", "")) == credential_id
                and str(getattr(row, "owner_name", "") or "") == owner_name
            ),
            None,
        )
        if exact is None:
            await interaction.response.send_message(
                "This reconnect control is stale; select the account again.", ephemeral=True
            )
            return
        await self._start_oauth(
            interaction, "reconnect", owner_name, credential_id, provider=provider
        )

    async def _start_oauth(
        self,
        interaction: Any,
        operation: str,
        owner_name: str,
        credential_id: str | None,
        *,
        provider: str | None = None,
    ) -> None:
        stable_provider = provider or self.selected_provider
        if stable_provider not in {"openai-codex", "anthropic"}:
            await interaction.response.send_message("Select a provider first.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        runner = self._ensure_runner()
        try:
            snapshot_store = getattr(self.adapter, "_account_control_snapshot_store", None)
            before_ids = (
                await asyncio.to_thread(snapshot_store.pool_ids, stable_provider)
                if snapshot_store is not None
                else ()
            )
            attempt = await asyncio.to_thread(
                runner.create,
                stable_provider,
                operation,
                owner_name,
                credential_id,
                ACCOUNT_CONTROL_OWNER_ID,
                ACCOUNT_CONTROL_GUILD_ID,
                self.channel_id,
            )
            if snapshot_store is not None:
                await asyncio.to_thread(
                    snapshot_store.capture,
                    attempt.attempt_id,
                    stable_provider,
                    before_ids,
                )
            started = await asyncio.to_thread(runner.start, attempt.attempt_id)
        except Exception as exc:  # noqa: BLE001 - runner seams expose no common base
            await interaction.followup.send(
                f"OAuth attempt could not start ({type(exc).__name__}).", ephemeral=True
            )
            return
        self.selected_attempt_id = attempt.attempt_id
        payload = await self._wait_for_oauth_result(runner, attempt)
        text = self._oauth_instructions(attempt, payload)
        kwargs: dict[str, Any] = {"ephemeral": True}
        if discord and stable_provider == "anthropic":
            kwargs["view"] = ClaudeSubmitView(self, stable_provider, attempt.attempt_id)
        await interaction.followup.send(text, **kwargs)
        del started

    async def _wait_for_oauth_result(self, runner: Any, attempt: Any) -> dict[str, Any]:
        """Wait briefly for the sibling runner's redacted URL/code artifact."""
        payload: dict[str, Any] = {}
        for index in range(100):
            payload = await asyncio.to_thread(self._oauth_result, runner, attempt)
            if payload.get("authorization_url"):
                return payload
            if payload.get("status") in {"cancelled", "expired", "failed", "succeeded"}:
                return payload
            if index < 99:
                await asyncio.sleep(0.1)
        return payload

    @staticmethod
    def _oauth_result(runner: Any, attempt: Any) -> dict[str, Any]:
        seam = getattr(runner, "oauth_result", None)
        if callable(seam):
            payload = seam(attempt)
            return payload if isinstance(payload, dict) else {}
        result_path = getattr(runner, "result_path", lambda _attempt: None)(attempt)
        if result_path is None:
            return {}
        try:
            with open(result_path, encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, TypeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _oauth_instructions(self, attempt: Any, payload: dict[str, Any]) -> str:
        raw_url = str(payload.get("authorization_url") or "")
        parsed = urlparse(raw_url)
        safe_url = ""
        provider = str(getattr(attempt, "provider", "") or "")
        rules = _OAUTH_URL_RULES.get(provider, {})
        query = parse_qsl(parsed.query, keep_blank_values=True)
        keys = {key.casefold() for key, _value in query}
        if (
            len(raw_url) <= 1000
            and parsed.scheme == "https"
            and parsed.hostname in rules
            and parsed.port in {None, 443}
            and parsed.path in rules.get(parsed.hostname, set())
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and keys <= _SAFE_OAUTH_QUERY_KEYS
        ):
            safe_url = urlunparse(
                ("https", parsed.hostname, parsed.path, "", urlencode(query), "")
            )
        raw_device_code = str(payload.get("device_code") or "")
        device_code = raw_device_code if _SAFE_DEVICE_CODE.fullmatch(raw_device_code) else ""
        lines = [
            f"Alias: `{attempt.owner_name}`",
            f"Expires: `{int(float(attempt.expires_at))}`",
        ]
        if safe_url:
            lines.append(f"Verification URL: {safe_url}")
        if provider == "openai-codex" and device_code:
            lines.append(f"Device code: `{device_code}`")
        if provider == "anthropic":
            lines.append("Open the URL, then use **Submit code** once.")
        return "\n".join(lines)

    async def close_attempt(self, interaction: Any) -> None:
        if not self.selected_attempt_id:
            await interaction.response.send_message("No live OAuth attempt is selected.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        attempt_id = self.selected_attempt_id
        cancelled = await asyncio.to_thread(self._ensure_runner().cancel, attempt_id)
        if cancelled:
            self.selected_attempt_id = ""
        await interaction.followup.send(
            "OAuth attempt closed." if cancelled else "OAuth attempt was already closed.",
            ephemeral=True,
        )

    async def submit_claude_code(
        self,
        interaction: Any,
        code: str,
        *,
        attempt_id: str | None = None,
        provider: str | None = None,
    ) -> None:
        if not await self._authorized(interaction):
            return
        stable_attempt_id = attempt_id or self.selected_attempt_id
        stable_provider = provider or self.selected_provider
        if (
            stable_provider != "anthropic"
            or not stable_attempt_id
            or (attempt_id is not None and self.selected_attempt_id != attempt_id)
        ):
            await interaction.response.send_message(
                "The selected OAuth attempt changed. Open a new code control.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            submitted = await asyncio.to_thread(
                self._ensure_runner().submit_claude_code,
                stable_attempt_id,
                str(code),
                user_id=ACCOUNT_CONTROL_OWNER_ID,
                channel_id=self.channel_id,
            )
        except Exception as exc:  # noqa: BLE001 - runner seam has no common error base
            await self._safe_failure(interaction, exc)
            return
        await interaction.followup.send(
            "Claude code submitted once." if submitted else "Claude code was rejected or expired.",
            ephemeral=True,
        )

    async def _finalize_succeeded_attempt(self) -> str:
        if not self.selected_attempt_id or self.coordinator is None or self.runner is None:
            return "Account roster refreshed."
        store = getattr(self.runner, "store", None)
        if store is None:
            return "Account roster refreshed."
        attempt = await asyncio.to_thread(store.get, self.selected_attempt_id)
        if attempt is None or getattr(attempt, "status", None) != "succeeded":
            return "Account roster refreshed."
        result = await asyncio.to_thread(
            self.coordinator.finalize, self.selected_attempt_id
        )
        status = getattr(result, "status", "")
        if status in {"committed", "rolled_back"}:
            self.selected_attempt_id = ""
        if status == "committed":
            await self.adapter.refresh_account_surfaces(reason="account-transaction")
            self._account_surfaces_refreshed = True
        return {
            "committed": "Credential transaction committed. Account roster refreshed.",
            "rolled_back": "Credential transaction rolled back safely. Account roster refreshed.",
            "reconciliation_required": (
                "Credential transaction needs attention; manual reconciliation is required."
            ),
            "presentation_reconciliation_pending": (
                "Credential change committed; presentation refresh is pending."
            ),
        }.get(status, "Account roster refreshed.")

    async def refresh_message(self) -> str:
        self._account_surfaces_refreshed = False
        outcome = await self._finalize_succeeded_attempt()
        if self._account_surfaces_refreshed:
            return outcome
        self.records = await asyncio.to_thread(_load_records, self.adapter)
        if discord:
            self._build_components()
        content = _render_records(self.records, selected_provider=self.selected_provider)
        if self.message is not None:
            await self.message.edit(content=content, view=self)
        return outcome


if discord:
    class OwnerNicknameModal(discord.ui.Modal, title="Add provider account"):
        owner_name = discord.ui.TextInput(
            label="Owner nickname",
            placeholder="Technical alias only",
            min_length=1,
            max_length=64,
        )

        def __init__(self, parent: AccountControlView):
            super().__init__(timeout=300, custom_id="agkacct:add-modal")
            self.parent = parent
            self.provider = parent.selected_provider

        async def on_submit(self, interaction: Any) -> None:
            await self.parent.start_add_bound(
                interaction, str(self.owner_name), self.provider
            )


    class ClaudeCodeModal(discord.ui.Modal, title="Submit Claude code"):
        code = discord.ui.TextInput(
            label="One-time code",
            min_length=1,
            max_length=4000,
        )

        def __init__(
            self, parent: AccountControlView, provider: str, attempt_id: str
        ):
            super().__init__(timeout=300, custom_id="agkacct:claude-code-modal")
            self.parent = parent
            self.provider = provider
            self.attempt_id = attempt_id

        async def on_submit(self, interaction: Any) -> None:
            if not await self.parent._authorized(interaction):
                return
            if self.parent.selected_attempt_id != self.attempt_id:
                await interaction.response.send_message(
                    "The selected OAuth attempt changed. Open a new code modal.", ephemeral=True
                )
                return
            await self.parent.submit_claude_code(
                interaction,
                str(self.code),
                attempt_id=self.attempt_id,
                provider=self.provider,
            )


    class ReconnectConfirmView(discord.ui.View):
        def __init__(
            self,
            parent: AccountControlView,
            provider: str,
            credential_id: str,
            owner_name: str,
        ):
            super().__init__(timeout=60)
            self.parent = parent
            self.provider = provider
            self.credential_id = credential_id
            self.owner_name = owner_name
            confirm = discord.ui.Button(
                label="Confirm reconnect",
                style=discord.ButtonStyle.danger,
                custom_id="agkacct:confirm-reconnect",
            )
            confirm.callback = self._confirm
            self.add_item(confirm)

        async def _confirm(self, interaction: Any) -> None:
            await self.parent.start_reconnect_bound(
                interaction, self.provider, self.credential_id, self.owner_name
            )


    class ClaudeSubmitView(discord.ui.View):
        def __init__(
            self, parent: AccountControlView, provider: str, attempt_id: str
        ):
            super().__init__(timeout=900)
            self.parent = parent
            self.provider = provider
            self.attempt_id = attempt_id
            submit = discord.ui.Button(
                label="Submit code",
                style=discord.ButtonStyle.primary,
                custom_id="agkacct:claude-code",
            )
            submit.callback = self._open_modal
            self.add_item(submit)

        async def _open_modal(self, interaction: Any) -> None:
            if not await self.parent._authorized(interaction):
                return
            if self.parent.selected_attempt_id != self.attempt_id:
                await interaction.response.send_message(
                    "The selected OAuth attempt changed. Open a new code control.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(
                ClaudeCodeModal(self.parent, self.provider, self.attempt_id)
            )
else:  # pragma: no cover - placeholders keep direct source tests importable
    OwnerNicknameModal = ClaudeCodeModal = ReconnectConfirmView = ClaudeSubmitView = object


async def _fetch_message(channel: Any, message_id: int | None):
    if not message_id:
        return None
    try:
        return await channel.fetch_message(int(message_id))
    except Exception as exc:  # noqa: BLE001 - discord may be optional in source tests
        not_found = (
            (discord is not None and isinstance(exc, discord.NotFound))
            or type(exc).__name__ == "NotFound"
        )
        if not_found:
            return None
        raise


async def _reconcile_account_control_channel(guild: Any, adapter: Any) -> AccountControlState:
    """Adopt only the exact bound channel/post and enforce its complete ACL."""
    if int(getattr(guild, "id", 0)) != ACCOUNT_CONTROL_GUILD_ID:
        raise PermissionError("account control center belongs to the exact Station guild")

    owner = guild.get_member(ACCOUNT_CONTROL_OWNER_ID) or _snowflake(ACCOUNT_CONTROL_OWNER_ID)
    channel = guild.get_channel(ACCOUNT_CONTROL_CHANNEL_ID)
    if channel is None:
        raise RuntimeError("exact account control channel is unavailable; refusing replacement")

    if getattr(channel, "guild", guild) is not guild and int(channel.guild.id) != ACCOUNT_CONTROL_GUILD_ID:
        raise PermissionError("account control channel escaped the Station guild")
    category = guild.get_channel(ACCOUNT_CONTROL_CATEGORY_ID)
    if category is None:
        raise RuntimeError("exact account control category is unavailable")
    channel_category = getattr(channel, "category", None)
    channel_type = getattr(getattr(channel, "type", None), "name", getattr(channel, "type", None))
    if (
        getattr(channel, "name", None) != ACCOUNT_CONTROL_CHANNEL_NAME
        or int(getattr(channel_category, "id", 0)) != ACCOUNT_CONTROL_CATEGORY_ID
        or channel_type not in {"text", "text_channel"}
    ):
        raise RuntimeError("exact account control channel binding is invalid")

    exact_overwrites = {
        guild.default_role: _permission(view_channel=False),
        owner: _permission(view_channel=True, read_message_history=True, send_messages=True),
    }
    bot_member = getattr(guild, "me", None)
    if bot_member is None:
        raise RuntimeError("Discord bot member is unavailable")
    exact_overwrites[bot_member] = _permission(
        view_channel=True,
        read_message_history=True,
        send_messages=True,
        manage_messages=True,
    )
    edited_channel = await channel.edit(
        overwrites=exact_overwrites,
        reason="Enforce exact private AGK account control ACL",
    )
    if edited_channel is not None:
        channel = edited_channel
    applied = getattr(channel, "overwrites", {})
    expected_ids = {int(target.id) for target in exact_overwrites}
    applied_by_id = {int(target.id): overwrite for target, overwrite in applied.items()}
    if set(applied_by_id) != expected_ids:
        raise PermissionError("account control ACL readback contains unauthorized targets")
    if (
        getattr(applied_by_id[int(guild.default_role.id)], "view_channel", None) is not False
        or getattr(applied_by_id[int(owner.id)], "view_channel", None) is not True
        or getattr(applied_by_id[int(bot_member.id)], "view_channel", None) is not True
    ):
        raise PermissionError("account control ACL readback is not private")

    message = await _fetch_message(channel, ACCOUNT_CONTROL_MESSAGE_ID)
    view = getattr(adapter, "_account_control_view", None)
    if view is None:
        view = AccountControlView(adapter, channel_id=int(channel.id))
        adapter._account_control_view = view
    view.channel_id = int(channel.id)
    content = await asyncio.to_thread(_render, adapter)
    if message is None:
        raise RuntimeError("exact account control post is unavailable; refusing replacement")
    else:
        await message.edit(content=content, view=view)
    if not getattr(message, "pinned", False):
        await message.pin(reason="Persistent AGK account control center")
    view.message = message
    state = AccountControlState(channel_id=int(channel.id), message_id=int(message.id))
    _write_state(adapter, state)
    return state


async def reconcile_account_control_channel(guild: Any, adapter: Any) -> AccountControlState:
    """Serialize reconciliation so overlapping ready events cannot duplicate work."""
    lock = getattr(adapter, "_account_control_reconcile_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        adapter._account_control_reconcile_lock = lock
    async with lock:
        return await _reconcile_account_control_channel(guild, adapter)


def register_account_control_center(bot: Any, adapter: Any) -> None:
    """Register the durable view once; reconciliation binds its one message."""
    if getattr(adapter, "_account_control_view_registered", False):
        return
    runner = getattr(adapter, "_account_control_oauth_runner", None)
    coordinator = getattr(adapter, "_account_control_coordinator", None)
    snapshot_store = getattr(adapter, "_account_control_snapshot_store", None)
    if runner is None or coordinator is None or snapshot_store is None:
        services = _build_account_control_services(adapter)
        adapter._account_control_oauth_runner = services.runner
        adapter._account_control_coordinator = services.coordinator
        adapter._account_control_snapshot_store = services.snapshot_store
        runner, coordinator = services.runner, services.coordinator
    view = getattr(adapter, "_account_control_view", None)
    if view is None:
        view = AccountControlView(adapter, runner=runner, coordinator=coordinator)
        adapter._account_control_view = view
    else:
        view.runner = runner
        view.coordinator = coordinator
    bot.add_view(view)
    adapter._account_control_view_registered = True
