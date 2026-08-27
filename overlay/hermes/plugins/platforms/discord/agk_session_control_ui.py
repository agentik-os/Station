"""Native Discord Session Control Center for the AGK Station."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

try:
    import discord
except ImportError:  # pragma: no cover
    discord = None

try:
    from .agk_session_control import (
        ControlError,
        StationSessionController,
        channel_allowed,
        confirmation_token,
        parse_target,
        progress_label,
    )
except ImportError:  # pragma: no cover
    from agk_session_control import (
        ControlError,
        StationSessionController,
        channel_allowed,
        confirmation_token,
        parse_target,
        progress_label,
    )


async def _authorized(adapter, interaction) -> bool:
    """Re-check both Discord authorization and the dedicated channel on every action."""
    _actor = interaction.user
    if not channel_allowed(interaction.channel_id):
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "This control center is restricted to <#1542462952714670190>.", ephemeral=True
            )
        return False
    return await adapter._check_slash_authorization(interaction, "/station-sessions")


def _target_for(record) -> str:
    if record.runtime_id:
        return f"runtime:{record.environment}:{record.runtime_id}"
    return f"hermes:{record.environment}:{record.hermes_session_id}"


def _age(timestamp: float) -> str:
    if not timestamp:
        return "unknown"
    seconds = max(0, int(datetime.now(timezone.utc).timestamp() - timestamp))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _embed(discord_module, records, selected=None, page=0):
    active = sum(row.status in {"running", "working", "idle"} for row in records)
    plans = sum(bool(row.progress.total) for row in records)
    embed = discord_module.Embed(
        title="Station · Session Control Center",
        description=(
            f"Hermes + AGK sessions across all agents\n"
            f"**{len(records)}** visible · **{active}** active · **{plans}** applied plans"
        ),
        color=discord_module.Color.blue(),
    )
    if selected is not None:
        kind = "AGK runtime" if selected.runtime_id else "Hermes session"
        embed.add_field(
            name=f"{selected.environment.upper()} · {selected.display_name[:80]}",
            value=(
                f"Type: `{kind}` · Runtime: `{selected.runtime_type}`\n"
                f"Status: `{selected.status}` · Activity: `{_age(selected.last_activity)}`\n"
                f"Profile: `{selected.profile}`\n"
                f"Plan progress: `{progress_label(selected.progress)}`"
            ),
            inline=False,
        )
    embed.set_footer(text=f"Page {page + 1} · Select a session, then choose an action")
    return embed


class PromptModal(discord.ui.Modal if discord else object, title="Launch prompt"):
    if discord:
        prompt = discord.ui.TextInput(
            label="Prompt for the selected live runtime",
            style=discord.TextStyle.paragraph,
            min_length=1,
            max_length=4000,
        )

    def __init__(self, view):
        super().__init__()
        self.control_view = view
        self.target = view.selected_target()

    async def on_submit(self, interaction):
        if not await _authorized(self.control_view.adapter, interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(
                self.control_view.controller.send_prompt,
                self.target,
                str(self.prompt),
            )
        except ControlError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send("Prompt delivered to the live runtime.", ephemeral=True)
        await self.control_view.refresh_message()


class ConfirmActionView(discord.ui.View if discord else object):
    def __init__(self, parent, action: str):
        super().__init__(timeout=60)
        self.parent = parent
        self.action = action
        self.target = parent.selected_target()
        confirm = discord.ui.Button(label=f"Confirm {action}", style=discord.ButtonStyle.danger, custom_id=f"station_confirm_{action}")
        confirm.callback = self._confirm
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, custom_id="station_confirm_cancel")
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def _confirm(self, interaction):
        if not await _authorized(self.parent.adapter, interaction):
            return
        await interaction.response.defer()
        token = confirmation_token(self.target)
        try:
            if self.target.kind == "runtime":
                result = await asyncio.to_thread(
                    self.parent.controller.apply_runtime_action,
                    self.target,
                    self.action,
                    token,
                )
            elif self.action == "archive":
                await asyncio.to_thread(self.parent.controller.archive_hermes, self.target)
                result = "Hermes session archived."
            elif self.action == "delete":
                await asyncio.to_thread(self.parent.controller.delete_hermes, self.target, token)
                result = "Hermes session deleted."
            else:
                raise ControlError("Stop requires a live AGK runtime")
        except ControlError as exc:
            await interaction.edit_original_response(content=str(exc), view=None)
            return
        await interaction.edit_original_response(content=str(result)[:1900], view=None)
        await self.parent.reload()
        await self.parent.refresh_message()

    async def _cancel(self, interaction):
        if not await _authorized(self.parent.adapter, interaction):
            return
        await interaction.response.edit_message(content="Action cancelled.", view=None)


class StationSessionView(discord.ui.View if discord else object):
    page_size = 25

    def __init__(self, adapter, controller=None):
        super().__init__(timeout=900)
        self.adapter = adapter
        self.controller = controller or StationSessionController()
        self.records = []
        self.page = 0
        self.selected = ""
        self.message = None

    async def load(self):
        self.records = await asyncio.to_thread(self.controller.list_sessions)
        self.page = min(self.page, max(0, (len(self.records) - 1) // self.page_size))
        self._build()

    async def reload(self):
        await self.load()

    def selected_record(self):
        return next((row for row in self.records if _target_for(row) == self.selected), None)

    def selected_target(self):
        if not self.selected:
            raise ControlError("Select a session first")
        return parse_target(self.selected)

    def _build(self):
        self.clear_items()
        selected_record = self.selected_record()
        has_live_runtime = bool(selected_record and selected_record.can_prompt)
        start = self.page * self.page_size
        page_rows = self.records[start:start + self.page_size]
        if page_rows:
            options = []
            for row in page_rows:
                label = f"{row.environment} · {row.display_name}"[:100]
                desc = f"{row.status} · {row.runtime_type} · {progress_label(row.progress)}"[:100]
                options.append(discord.SelectOption(label=label, value=_target_for(row), description=desc, default=_target_for(row) == self.selected))
            select = discord.ui.Select(placeholder="Choose a Hermes or AGK session…", options=options, custom_id="station_session_select")
            select.callback = self._select
            self.add_item(select)
        buttons = [
            ("Refresh", discord.ButtonStyle.primary, self._refresh, False),
            ("Logs", discord.ButtonStyle.secondary, self._logs, not bool(self.selected)),
            ("Prompt", discord.ButtonStyle.success, self._prompt, not has_live_runtime),
            ("Stop", discord.ButtonStyle.danger, self._stop, not has_live_runtime),
            ("Archive", discord.ButtonStyle.secondary, self._archive, not bool(self.selected)),
            ("Delete", discord.ButtonStyle.danger, self._delete, not bool(self.selected)),
            ("Close", discord.ButtonStyle.secondary, self._close, False),
        ]
        if self.page > 0:
            buttons.insert(0, ("Previous", discord.ButtonStyle.secondary, self._previous, False))
        if start + self.page_size < len(self.records):
            buttons.insert(1, ("Next", discord.ButtonStyle.secondary, self._next, False))
        for index, (label, style, callback, disabled) in enumerate(buttons):
            button = discord.ui.Button(label=label, style=style, custom_id=f"station_{label.lower()}", disabled=disabled, row=1 + index // 5)
            button.callback = callback
            self.add_item(button)

    async def _select(self, interaction):
        if not await _authorized(self.adapter, interaction):
            return
        self.selected = interaction.data["values"][0]
        self._build()
        await interaction.response.edit_message(embed=_embed(discord, self.records, self.selected_record(), self.page), view=self)

    async def _refresh(self, interaction):
        if not await _authorized(self.adapter, interaction): return
        await interaction.response.defer()
        await self.reload()
        await interaction.edit_original_response(embed=_embed(discord, self.records, self.selected_record(), self.page), view=self)

    async def _logs(self, interaction):
        if not await _authorized(self.adapter, interaction): return
        await interaction.response.defer(ephemeral=True)
        try: logs = await asyncio.to_thread(self.controller.logs, self.selected_target())
        except ControlError as exc: logs = str(exc)
        await interaction.followup.send(f"```text\n{logs[-1800:]}\n```", ephemeral=True)

    async def _prompt(self, interaction):
        if not await _authorized(self.adapter, interaction): return
        try:
            target = self.selected_target()
            self.controller.validate_prompt(target, "probe")
        except ControlError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True); return
        await interaction.response.send_modal(PromptModal(self))

    async def _confirm(self, interaction, action):
        if not await _authorized(self.adapter, interaction): return
        await interaction.response.send_message(
            f"Confirm **{action}** for `{self.selected}`?",
            view=ConfirmActionView(self, action),
            ephemeral=True,
        )

    async def _stop(self, interaction): await self._confirm(interaction, "stop")
    async def _archive(self, interaction): await self._confirm(interaction, "archive")
    async def _delete(self, interaction): await self._confirm(interaction, "delete")

    async def _previous(self, interaction):
        if not await _authorized(self.adapter, interaction): return
        self.page = max(0, self.page - 1); self._build()
        await interaction.response.edit_message(embed=_embed(discord, self.records, self.selected_record(), self.page), view=self)

    async def _next(self, interaction):
        if not await _authorized(self.adapter, interaction): return
        self.page += 1; self._build()
        await interaction.response.edit_message(embed=_embed(discord, self.records, self.selected_record(), self.page), view=self)

    async def _close(self, interaction):
        if not await _authorized(self.adapter, interaction): return
        self.stop()
        await interaction.response.edit_message(content="Session Control Center closed.", embed=None, view=None)

    async def refresh_message(self):
        if self.message:
            await self.message.edit(embed=_embed(discord, self.records, self.selected_record(), self.page), view=self)


def register_station_session_commands(adapter, tree) -> bool:
    if discord is None:
        return False
    configured = int(adapter.config.extra.get("session_manager_channel_id", 0) or 0)
    if configured != 1542462952714670190:
        return False

    @tree.command(name="station-sessions", description="Monitor and control Hermes + AGK sessions")
    async def station_sessions(interaction: discord.Interaction):
        if not await _authorized(adapter, interaction):
            return
        view = StationSessionView(adapter)
        try:
            await view.load()
        except Exception as exc:
            await interaction.response.send_message(f"Session catalog unavailable: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(embed=_embed(discord, view.records, None, 0), view=view, ephemeral=True)
        view.message = await interaction.original_response()

    return True
