"""Registry-driven Discord `/os` control center."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import os
import json
from pathlib import Path
import re
import sys
from typing import Callable, Iterable
from urllib.parse import urlencode

import discord
import yaml
from discord import app_commands


@dataclass(frozen=True)
class OsViewRecord:
    os_id: str
    name: str
    version: str
    owner_environment: str
    profile_id: str
    profile_state: str
    agent_state: str
    discord_mode: str
    discord_state: str
    doctor_state: str


def station_ui_command_names(hermes_home: Path) -> set[str]:
    """Return the only actionable slash commands owned by one Station bot."""
    home = Path(hermes_home).resolve()
    names = {"panel", "settings", "clear", "voice", "restart", "leave"}
    if home == Path("/home/operator/.hermes"):
        names.add("station-sessions")
    elif home == Path("/home/private/.hermes"):
        names.add("os")
    elif home == Path("/home/private/.hermes/profiles/nutrition-os"):
        names.update({"nutrition", "food"})
    return names


def validate_private_os_record(
    record: OsViewRecord,
    profiles_root: Path = Path("/home/private/.hermes/profiles"),
) -> Path:
    if (
        record.owner_environment != "private"
        or record.os_id != record.profile_id
        or not _SAFE_ID.fullmatch(str(record.profile_id))
    ):
        raise ValueError("Discord setup is limited to canonical Private-owned OS profiles")
    root = Path(profiles_root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Private profiles root is unsafe")
    profile = root / record.profile_id
    if profile.parent != root or profile.resolve().parent != root.resolve():
        raise ValueError("Private OS profile escapes the canonical profiles root")
    distribution = profile / "distribution.yaml"
    if profile.is_symlink() or distribution.is_symlink() or not distribution.is_file():
        raise ValueError("Private OS profile distribution is unavailable")
    try:
        manifest = yaml.safe_load(distribution.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("Private OS profile distribution is invalid") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("owner_environment") != "private"
        or manifest.get("profile_id") != record.profile_id
        or str(manifest.get("os_id") or "") != record.os_id
        or str(manifest.get("version") or "") != record.version
    ):
        raise ValueError("Private OS profile distribution does not match the registry")
    return profile


def records_from_snapshot(path: Path) -> list[OsViewRecord]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    organisations = payload.get("organisations") if isinstance(payload, dict) else {}
    if not isinstance(organisations, dict):
        return []
    rows: dict[str, OsViewRecord] = {}
    for environment, station in organisations.items():
        if not isinstance(station, dict):
            continue
        for item in station.get("os") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            owner_value = item.get("owner_environment")
            if not owner_value:
                continue
            owner = str(owner_value)
            if owner != environment:
                continue
            record = OsViewRecord(
                os_id=str(item["id"]), name=str(item.get("name") or item["id"]),
                version=str(item.get("version") or ""), owner_environment=owner,
                profile_id=str(item.get("profile_id") or item["id"]),
                profile_state=str(item.get("profile_state") or "missing"),
                agent_state=str(item.get("agent_state") or "missing"),
                discord_mode=str(item.get("discord_mode") or "disabled"),
                discord_state=str(item.get("discord_state") or "disabled"),
                doctor_state=str(item.get("doctor_state") or "blocked"),
            )
            rows[record.os_id] = record
    return sorted(rows.values(), key=lambda row: (row.owner_environment, row.name.casefold(), row.os_id))


def _next_action(record: OsViewRecord) -> str:
    if record.profile_state != "ready":
        return "Install profile"
    if record.agent_state != "ready":
        return "Bind owning agent"
    if record.doctor_state != "ready":
        return "Run doctor"
    if record.discord_state == "owner-prerequisite":
        return "Complete Discord OAuth"
    return "No action"


class OsSelect(discord.ui.Select):
    def __init__(self, parent: "OsControlView"):
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label=record.name[:100],
                value=record.os_id,
                description=f"{record.owner_environment} · {record.profile_state} · {record.version}"[:100],
                default=record.os_id == parent.selected_id,
            )
            for record in parent.page_records[:25]
        ]
        if parent.has_next_page and len(options) == 25:
            options[-1] = discord.SelectOption(label="Next page", value="__next__", description="Show more OS entries")
        super().__init__(placeholder="Choose an Operative System", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        value = self.values[0]
        if value == "__next__":
            self.parent_view.page += 1
            self.parent_view.selected_id = self.parent_view.page_records[0].os_id
        else:
            self.parent_view.selected_id = value
        self.parent_view.rebuild()
        await interaction.response.edit_message(content=self.parent_view.render_content(), view=self.parent_view)


class OsControlView(discord.ui.View):
    page_size = 25

    def __init__(
        self,
        records: Iterable[OsViewRecord],
        *,
        owner_ids: set[int],
        timeout: float = 900,
        state_path: Path = Path("/home/private/.hermes/os-control-state.json"),
        membership_checker=None,
        route_launcher=None,
        channel_resolver=None,
        record_validator=None,
    ):
        super().__init__(timeout=timeout)
        self.records = tuple(sorted(records, key=lambda row: (row.owner_environment, row.name.casefold(), row.os_id)))
        self.owner_ids = {int(value) for value in owner_ids}
        self.state_path = Path(state_path)
        self.membership_checker = membership_checker or _default_membership_checker
        self.route_launcher = route_launcher or _default_route_launcher
        self.channel_resolver = channel_resolver or (
            lambda os_id: load_home_channel(Path("/var/lib/agk-terminal/fleet/os-discord-channels.json"), os_id)
        )
        self.record_validator = record_validator or validate_private_os_record
        self.page = 0
        self.selected_id = self.records[0].os_id if self.records else ""
        self.rebuild()

    @property
    def page_records(self) -> tuple[OsViewRecord, ...]:
        start = self.page * self.page_size
        return self.records[start:start + self.page_size]

    @property
    def has_next_page(self) -> bool:
        return (self.page + 1) * self.page_size < len(self.records)

    @property
    def selected(self) -> OsViewRecord | None:
        return next((record for record in self.records if record.os_id == self.selected_id), None)

    def rebuild(self) -> None:
        self.clear_items()
        if self.records:
            self.add_item(OsSelect(self))
        for item in (self.install, self.repair, self.doctor, self.discord_setup, self.refresh, self.back, self.close):
            self.add_item(item)

    def render_content(self) -> str:
        record = self.selected
        if record is None:
            return "**OS Control**\nNo registered Operative Systems."
        return "\n".join([
            f"**{record.name} · {record.version}**",
            f"Owner: `{record.owner_environment}` · Profile: {record.profile_state} · Agent: {record.agent_state}",
            f"Discord: {record.discord_mode}/{record.discord_state} · Doctor: {record.doctor_state}",
            f"Next: {_next_action(record)}",
        ])

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if int(interaction.user.id) in self.owner_ids and int(getattr(interaction, "guild_id", 0) or 0) == int(AGK_GUILD_ID):
            return True
        await interaction.response.send_message("Not authorized", ephemeral=True)
        return False

    async def _read_only(self, interaction: discord.Interaction, action: str) -> None:
        await interaction.response.send_message(f"{action} requires a staged confirmation.", ephemeral=True)

    @discord.ui.button(label="Install", style=discord.ButtonStyle.primary, row=1)
    async def install(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._read_only(interaction, "Install")

    @discord.ui.button(label="Repair", style=discord.ButtonStyle.secondary, row=1)
    async def repair(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._read_only(interaction, "Repair")

    @discord.ui.button(label="Doctor", style=discord.ButtonStyle.secondary, row=1)
    async def doctor(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._read_only(interaction, "Doctor")

    @discord.ui.button(label="Discord", style=discord.ButtonStyle.secondary, row=1)
    async def discord_setup(self, interaction: discord.Interaction, _button: discord.ui.Button):
        record = self.selected
        if record is None or record.discord_mode != "dedicated":
            await interaction.response.send_message(
                f"Discord mode: {record.discord_mode if record else 'unavailable'}.", ephemeral=True
            )
            return
        if record.owner_environment != "private":
            await interaction.response.send_message("Discord setup is limited to Private-owned OS profiles.", ephemeral=True)
            return
        try:
            self.record_validator(record)
        except ValueError:
            await interaction.response.send_message("Private OS profile validation failed.", ephemeral=True)
            return
        current = load_application_id(self.state_path, record.os_id)
        await interaction.response.send_modal(ApplicationIdModal(self, current))

    async def complete_discord_setup(self, interaction: discord.Interaction, application_id: str) -> None:
        record = self.selected
        if record is None or record.discord_mode != "dedicated":
            await interaction.response.send_message("Dedicated Discord mode is not enabled.", ephemeral=True)
            return
        if record.owner_environment != "private":
            await interaction.response.send_message("Discord setup is limited to Private-owned OS profiles.", ephemeral=True)
            return
        try:
            self.record_validator(record)
        except ValueError:
            await interaction.response.send_message("Private OS profile validation failed.", ephemeral=True)
            return
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            persist_application_id(self.state_path, record.os_id, application_id)
            member = await self.membership_checker(interaction, str(application_id))
            if not member:
                await interaction.followup.send(
                    "Authorize the bot in AGK, then press Discord again: " + oauth_invite_url(str(application_id)),
                    ephemeral=True,
                )
                return
            home_channel = self.channel_resolver(record.os_id)
            url = await self.route_launcher(
                secure_input_installer_argv(
                    record, str(application_id), home_channel,
                    record_validator=self.record_validator,
                )
            )
        except (OSError, ValueError, RuntimeError, asyncio.TimeoutError):
            await interaction.followup.send("Discord setup failed safely. Refresh and retry.", ephemeral=True)
            return
        await interaction.followup.send(f"Secure Input ready: {url}", ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.rebuild()
        await interaction.response.edit_message(content=self.render_content(), view=self)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            self.selected_id = self.page_records[0].os_id
        self.rebuild()
        await interaction.response.edit_message(content=self.render_content(), view=self)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, row=2)
    async def close(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(content="OS Control closed.", view=None)


def register_os_control_center(
    bot,
    catalog_loader: Callable[[], Iterable[OsViewRecord]],
    *,
    owner_ids: set[int],
) -> app_commands.Command:
    async def command_callback(interaction: discord.Interaction):
        if (
            int(getattr(interaction.user, "id", 0) or 0) not in owner_ids
            or int(getattr(interaction, "guild_id", 0) or 0) != int(AGK_GUILD_ID)
        ):
            await interaction.response.send_message("Not authorized", ephemeral=True)
            return
        view = OsControlView(catalog_loader(), owner_ids=owner_ids)
        await interaction.response.send_message(view.render_content(), view=view, ephemeral=True)

    command = app_commands.Command(
        name="os",
        description="Inspect and operate registered AGK Operative Systems.",
        callback=command_callback,
    )
    bot.tree.add_command(command, override=True)
    return command
AGK_GUILD_ID = "1541131439599386644"
_SAFE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SNOWFLAKE = re.compile(r"^[0-9]{15,22}$")
_OWNER_UIDS = {"operator": 1000, "agentik": 1001, "mission": 1002, "private": 1003}


def load_home_channel(path: Path, os_id: str) -> str:
    if not _SAFE_ID.fullmatch(str(os_id)):
        raise ValueError("invalid OS identity")
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Discord home channel is unavailable") from exc
    channels = payload.get("channels") if isinstance(payload, dict) else None
    row = channels.get(os_id) if isinstance(channels, dict) else None
    channel = str(row.get("id") or "") if isinstance(row, dict) else ""
    if not _SNOWFLAKE.fullmatch(channel):
        raise ValueError("Discord home channel is unavailable")
    return channel


def secure_input_installer_argv(
    record: "OsViewRecord", application_id: str, home_channel: str,
    *, record_validator: Callable[[OsViewRecord], Path] = validate_private_os_record,
) -> list[str]:
    profile_path = record_validator(record)
    if (
        not _SAFE_ID.fullmatch(record.profile_id)
        or not _SNOWFLAKE.fullmatch(str(application_id))
        or not _SNOWFLAKE.fullmatch(str(home_channel))
    ):
        raise ValueError("invalid OS or Discord application identity")
    home = "/home/private"
    profile = str(profile_path)
    installer = [
        "/usr/local/lib/agk-terminal/scripts/install-discord-token.py",
        "--target", f"{profile}/.env",
        "--allowed-root", profile,
        "--expected-guild", AGK_GUILD_ID,
        "--expected-application", str(application_id),
        "--expected-os-id", record.os_id,
        "--expected-os-version", record.version,
        "--profile-id", record.profile_id,
        "--home-channel", str(home_channel),
    ]
    runtime = "/run/user/1003"
    return [
        "/usr/bin/env", "-i",
        f"HOME={home}", "HERMES_HOME=/home/private/.hermes",
        f"XDG_RUNTIME_DIR={runtime}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime}/bus",
        "PATH=/home/private/.local/bin:/opt/agk-terminal/hermes-agent/venv/bin:/usr/local/bin:/usr/bin:/bin",
        *installer,
    ]


def persist_application_id(path: Path, os_id: str, application_id: str) -> None:
    if not _SAFE_ID.fullmatch(str(os_id)) or not _SNOWFLAKE.fullmatch(str(application_id)):
        raise ValueError("invalid OS or Discord application identity")
    path = Path(path)
    if path.is_symlink():
        raise ValueError("unsafe public state path")
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        existing = {}
    applications = existing.get("applications") if isinstance(existing, dict) else {}
    applications = dict(applications) if isinstance(applications, dict) else {}
    applications[str(os_id)] = str(application_id)
    payload = {"applications": applications, "schema": "agk.os-control-public.v1"}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def load_application_id(path: Path, os_id: str) -> str:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    value = (payload.get("applications") or {}).get(os_id) if isinstance(payload, dict) else ""
    return str(value) if _SNOWFLAKE.fullmatch(str(value or "")) else ""


def oauth_invite_url(application_id: str) -> str:
    if not _SNOWFLAKE.fullmatch(str(application_id)):
        raise ValueError("invalid Discord application identity")
    return "https://discord.com/oauth2/authorize?" + urlencode({
        "client_id": str(application_id),
        "permissions": "2147601472",
        "scope": "bot applications.commands",
        "guild_id": AGK_GUILD_ID,
        "disable_guild_select": "true",
    })


async def _default_membership_checker(interaction, application_id: str) -> bool:
    guild = getattr(interaction, "guild", None)
    if guild is None or int(getattr(guild, "id", 0) or 0) != int(AGK_GUILD_ID):
        client = getattr(interaction, "client", None)
        guild = client.get_guild(int(AGK_GUILD_ID)) if client is not None else None
    if guild is None:
        raise RuntimeError("AGK guild is unavailable")
    try:
        await guild.fetch_member(int(application_id))
    except discord.NotFound:
        return False
    return True


_ROUTE_PROCESSES: set[asyncio.subprocess.Process] = set()


async def _default_route_launcher(installer_argv: list[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "/usr/local/lib/agk-terminal/scripts/tailnet_secure_input.py",
        "--installer-json", json.dumps(installer_argv),
        "--ttl", "1800",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    if process.stdout is None:
        raise RuntimeError("Secure Input did not start")
    try:
        line = await asyncio.wait_for(process.stdout.readline(), timeout=20)
    except asyncio.TimeoutError:
        process.terminate()
        await process.wait()
        raise
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        process.terminate()
        await process.wait()
        raise RuntimeError("Secure Input returned an invalid receipt") from exc
    url = str(payload.get("url") or "") if payload.get("status") == "READY" else ""
    if not url.startswith("https://"):
        process.terminate()
        await process.wait()
        raise RuntimeError("Secure Input did not provide Tailnet HTTPS")
    _ROUTE_PROCESSES.add(process)

    async def reap() -> None:
        try:
            await process.wait()
        finally:
            _ROUTE_PROCESSES.discard(process)

    asyncio.create_task(reap())
    return url


class ApplicationIdModal(discord.ui.Modal, title="Connect dedicated Discord bot"):
    application_id = discord.ui.TextInput(
        label="Application ID",
        placeholder="Public Discord Application ID",
        min_length=15,
        max_length=22,
    )

    def __init__(self, parent: OsControlView, current: str = ""):
        super().__init__(timeout=300, custom_id="agkos:discord-application")
        self.parent = parent
        if current:
            self.application_id.default = current

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.parent.interaction_check(interaction):
            return
        await self.parent.complete_discord_setup(interaction, str(self.application_id))
