"""Owner-scoped dynamic Discord controls for Hermes voice mode."""
from __future__ import annotations

import discord


VOICE_CONTROL_COPY = (
    "**Voice**\n"
    "Join the voice channel you are currently in, choose when replies are spoken, "
    "or disconnect. Only authorized users are accepted."
)


class VoiceReplyModeSelect(discord.ui.Select):
    def __init__(self, parent: "VoiceControlView"):
        self.parent_view = parent
        super().__init__(
            placeholder="Reply mode",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="Voice messages only",
                    value="on",
                    description="Speak only after a voice message.",
                ),
                discord.SelectOption(
                    label="Always speak",
                    value="tts",
                    description="Speak every reply.",
                ),
                discord.SelectOption(
                    label="Text only",
                    value="off",
                    description="Disable spoken chat replies.",
                ),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await self.parent_view.dispatch(interaction, f"/voice {self.values[0]}")


class VoiceControlView(discord.ui.View):
    def __init__(self, adapter, *, timeout: float = 900):
        super().__init__(timeout=timeout)
        self.adapter = adapter
        self.add_item(VoiceReplyModeSelect(self))

    async def dispatch(self, interaction: discord.Interaction, command: str) -> None:
        if not await self.adapter._check_slash_authorization(interaction, "/voice"):
            return
        await self.adapter._run_simple_slash(interaction, command)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.primary, row=1)
    async def join(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.dispatch(interaction, "/voice join")

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.secondary, row=1)
    async def leave(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.dispatch(interaction, "/voice leave")

    @discord.ui.button(label="Status", style=discord.ButtonStyle.secondary, row=1)
    async def status(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.dispatch(interaction, "/voice status")

    @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary, row=1)
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if not await self.adapter._check_slash_authorization(interaction, "/voice"):
            return
        await interaction.response.edit_message(content="Voice control closed.", view=None)


async def open_voice_control(adapter, interaction: discord.Interaction) -> bool:
    if not await adapter._check_slash_authorization(interaction, "/voice"):
        return False
    await interaction.response.send_message(
        VOICE_CONTROL_COPY,
        view=VoiceControlView(adapter),
        ephemeral=True,
    )
    return True
