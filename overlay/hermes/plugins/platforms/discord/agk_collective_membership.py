"""Collective membership UI inside the one canonical AGK Discord gateway."""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from typing import Any

try:
    import discord
except ImportError:  # pragma: no cover
    discord = None

SCRIPTS = Path("/usr/local/lib/agk-terminal/scripts")
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

GUILD_ID = 1350170767366688830
START_CHANNEL = 1541213937096458391
SIGN_MESSAGE = 1541725051248713808
NEW_MEMBERS_CHANNEL = 1541427859367788544
SIGNED_ROLE = 1541225509940109322
PRO_ROLE = 1541213916640841859
FREE_ROLE = 1541213914002489374
INTRO_URL = "https://form.typeform.com/to/xdQWd8Gv"
DEAL_URL = "https://form.typeform.com/to/r7DHpFxv"
PRO_URL = "https://buy.stripe.com/9B65kD0P32Jy1Vs2BD9R604?prefilled_promo_code=COLLECTIVE70"
SIGN_URL = f"https://discord.com/channels/{GUILD_ID}/{START_CHANNEL}/{SIGN_MESSAGE}"
CUSTOM_IDS = {
    "sign_house",
    "confirm_house",
    "sign_deals",
    "confirm_deals",
    "sign_conduct",
    "open_sign_modal",
    "terms_sign_modal",
    "collective_refresh",
    "collective_close",
}


def _enabled() -> bool:
    try:
        return Path(os.environ.get("HERMES_HOME", "")).resolve() == Path("/home/mission/.hermes/profiles/collective")
    except OSError:
        return False


def _store():
    from collective_automation_core import CollectiveStore

    return CollectiveStore(Path(os.environ["HERMES_HOME"]) / "collective-automation.db")


def _guild_ok(interaction: Any) -> bool:
    return int(getattr(interaction, "guild_id", 0) or 0) == GUILD_ID and not bool(getattr(getattr(interaction, "user", None), "bot", False))


def _button_view(*buttons: tuple[str, str, int]):
    view = discord.ui.View(timeout=900)
    for custom_id, label, style in buttons:
        view.add_item(discord.ui.Button(label=label, style=style, custom_id=custom_id))
    return view


def _panel_view():
    view = discord.ui.View(timeout=900)
    for label, url in (("Sign 1 → 2 → 3", SIGN_URL), ("Introduce myself", INTRO_URL), ("Submit a deal", DEAL_URL), ("Join Pro", PRO_URL)):
        view.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.link, url=url))
    view.add_item(discord.ui.Button(label="Refresh", style=discord.ButtonStyle.primary, custom_id="collective_refresh", row=1))
    view.add_item(discord.ui.Button(label="Close", style=discord.ButtonStyle.secondary, custom_id="collective_close", row=1))
    return view


def _panel_content(user_id: str) -> str:
    progress = _store().signature_progress(user_id)
    if progress["signed"]:
        signature = "Terms signed"
    else:
        done = len(progress["steps"])
        signature = f"Signature · {done}/3"
    return "\n".join(("# Collective", signature, "Signed opens COLLECTIVE. Pro unlocks LEARN · BUILD · EARN.", "Choose the next action below."))


async def _grant_signed(interaction: Any, result: dict[str, Any]) -> None:
    guild = getattr(interaction, "guild", None)
    if guild is None or guild.id != GUILD_ID:
        raise RuntimeError("wrong Collective guild")
    member = getattr(interaction, "user", None)
    if not isinstance(member, discord.Member):
        member = await guild.fetch_member(int(result["discord_id"]))
    role = guild.get_role(SIGNED_ROLE)
    if role is None:
        raise RuntimeError("Signed role unavailable")
    await member.add_roles(role, reason="Collective explicit I ACCEPT signature")
    reread = await guild.fetch_member(member.id)
    if SIGNED_ROLE not in {item.id for item in reread.roles}:
        raise RuntimeError("Signed role readback failed")


async def _handle_button(interaction: Any, custom_id: str) -> None:
    user_id = str(interaction.user.id)
    store = _store()
    if custom_id == "collective_close":
        await interaction.response.edit_message(content="Collective panel closed.", view=None)
        return
    if custom_id == "collective_refresh":
        await interaction.response.edit_message(content=_panel_content(user_id), view=_panel_view())
        return
    if custom_id == "sign_house":
        await interaction.response.send_message(
            "**Step 1 / 3 — House rules**\nBuild in public. No spam or cold pitches. Client contacts stay private. Be direct, not cruel. Confirm only if you accept these rules.",
            view=_button_view(("confirm_house", "I confirm — House rules", discord.ButtonStyle.success)),
            ephemeral=True,
        )
        return
    if custom_id == "confirm_house":
        await asyncio.to_thread(store.mark_signature_step, user_id, "house", f"discord:{interaction.id}:house")
        await interaction.response.edit_message(
            content="✅ **Step 1 / 3 confirmed.**\n\n**Step 2 / 3 — Deals & referrals**\nDefault paid member-sourced split: 5% Agentik · 15% Business Referrer · 80% Operator. Never bypass an introduction.",
            view=_button_view(("confirm_deals", "I confirm — Deals & referrals", discord.ButtonStyle.success)),
        )
        return
    if custom_id == "sign_deals":
        progress = await asyncio.to_thread(store.signature_progress, user_id)
        if "house" not in progress["steps"]:
            await interaction.response.send_message("Complete Step 1 first.", view=_button_view(("sign_house", "Go to Step 1", discord.ButtonStyle.primary)), ephemeral=True)
            return
        await interaction.response.send_message(
            "**Step 2 / 3 — Deals & referrals**\nDefault paid member-sourced split: 5% Agentik · 15% Business Referrer · 80% Operator. Never bypass an introduction.",
            view=_button_view(("confirm_deals", "I confirm — Deals & referrals", discord.ButtonStyle.success)),
            ephemeral=True,
        )
        return
    if custom_id == "confirm_deals":
        progress = await asyncio.to_thread(store.signature_progress, user_id)
        if "house" not in progress["steps"]:
            await interaction.response.send_message("Step 1 is still required.", ephemeral=True)
            return
        await asyncio.to_thread(store.mark_signature_step, user_id, "deals", f"discord:{interaction.id}:deals")
        await interaction.response.edit_message(
            content="✅ **Step 2 / 3 confirmed.**\n\nLast step: your name and the exact phrase **I ACCEPT**.",
            view=_button_view(("open_sign_modal", "Open signature", discord.ButtonStyle.primary)),
        )
        return
    if custom_id in {"sign_conduct", "open_sign_modal"}:
        progress = await asyncio.to_thread(store.signature_progress, user_id)
        missing = [step for step in ("house", "deals") if step not in progress["steps"]]
        if missing:
            next_id = "sign_house" if "house" in missing else "sign_deals"
            await interaction.response.send_message("Complete Steps 1 and 2 before signing.", view=_button_view((next_id, "Go to next open step", discord.ButtonStyle.primary)), ephemeral=True)
            return

        class SignatureModal(discord.ui.Modal, title="Sign Collective terms"):
            legal_name = discord.ui.TextInput(label="Your name", min_length=2, max_length=80, required=True)
            accept_phrase = discord.ui.TextInput(label="Type exactly: I ACCEPT", min_length=8, max_length=8, required=True)

            async def on_submit(self, modal_interaction: Any) -> None:
                if not _guild_ok(modal_interaction):
                    await modal_interaction.response.send_message("Not authorized for this guild.", ephemeral=True)
                    return
                result = await asyncio.to_thread(
                    store.complete_signature,
                    str(modal_interaction.user.id),
                    str(self.legal_name.value),
                    str(self.accept_phrase.value),
                    f"discord:{modal_interaction.id}:signature",
                )
                if not result.get("ok"):
                    await modal_interaction.response.send_message("Signature rejected. Type **I ACCEPT** exactly after completing Steps 1 and 2.", ephemeral=True)
                    return
                await modal_interaction.response.defer(ephemeral=True)
                await _grant_signed(modal_interaction, result)
                await modal_interaction.edit_original_response(content="✅ Signed. COLLECTIVE is open. Pro unlocks LEARN · BUILD · EARN.")

        await interaction.response.send_modal(SignatureModal())


def register_collective_membership_listener(bot: Any, adapter: Any) -> bool:
    if discord is None or not _enabled():
        return False

    @bot.listen("on_interaction")
    async def on_collective_interaction(interaction: Any) -> None:
        data = getattr(interaction, "data", None)
        custom_id = str(data.get("custom_id") or "") if isinstance(data, dict) else ""
        if custom_id not in CUSTOM_IDS or not _guild_ok(interaction):
            return
        if custom_id == "terms_sign_modal":
            return
        if custom_id.startswith(("sign_", "confirm_", "open_sign_")) and int(getattr(interaction, "channel_id", 0) or 0) != START_CHANNEL:
            return
        await _handle_button(interaction, custom_id)

    @bot.listen("on_raw_reaction_add")
    async def on_collective_reaction(payload: Any) -> None:
        if int(getattr(payload, "guild_id", 0) or 0) != GUILD_ID or int(getattr(payload, "message_id", 0) or 0) != SIGN_MESSAGE:
            return
        if str(getattr(payload, "emoji", "")) not in {"✅", "white_check_mark"}:
            return
        user_id = int(getattr(payload, "user_id", 0) or 0)
        if not user_id or user_id == getattr(getattr(bot, "user", None), "id", None):
            return
        event_id = f"discord:reaction:{getattr(payload, 'message_id', '')}:{user_id}"
        store = _store()
        claimed = await asyncio.to_thread(
            store.claim_event,
            event_id,
            "reaction_redirect",
            hashlib.sha256(event_id.encode("utf-8")).hexdigest(),
        )
        if not claimed:
            return
        try:
            user = bot.get_user(user_id) or await bot.fetch_user(user_id)
            await user.send("A reaction is no longer a signature. Use the **1 → 2 → 3** buttons in #start-here and type **I ACCEPT** explicitly.")
        except Exception as error:
            await asyncio.to_thread(store.mark_failed, event_id, error)
            raise
        await asyncio.to_thread(store.mark_delivered, event_id, "dm", "redirect")

    @bot.listen("on_member_join")
    async def on_collective_member_join(member: Any) -> None:
        if member.guild.id != GUILD_ID or member.bot:
            return
        store = _store()
        if await asyncio.to_thread(store.was_welcomed, str(member.id)):
            return
        if PRO_ROLE not in {role.id for role in member.roles}:
            role = member.guild.get_role(FREE_ROLE)
            if role:
                await member.add_roles(role, reason="Collective member joined")
        channel = member.guild.get_channel(NEW_MEMBERS_CHANNEL) or await bot.fetch_channel(NEW_MEMBERS_CHANNEL)
        content = f"<@{member.id}> — welcome to **Agentik**.\nRead <#1541218159032401952>, sign 1 → 2 → 3 in <#1541213937096458391>, then introduce yourself."
        message = await channel.send(content, allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
        reread = await channel.fetch_message(message.id)
        if reread.content != content:
            raise RuntimeError("Welcome readback mismatch")
        await asyncio.to_thread(store.mark_welcomed, str(member.id), str(message.id))

    return True


def register_collective_commands(adapter: Any, tree: Any) -> bool:
    if discord is None or not _enabled():
        return False

    @tree.command(name="collective", description="Open the Collective membership and community panel")
    async def collective(interaction: discord.Interaction):
        if not _guild_ok(interaction):
            await interaction.response.send_message("This panel belongs to the Collective guild.", ephemeral=True)
            return
        await interaction.response.send_message(content=_panel_content(str(interaction.user.id)), view=_panel_view(), ephemeral=True)

    return True
