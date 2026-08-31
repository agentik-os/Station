"""Connection-only Discord `/os` surface for PRIVATE Personal OS."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Callable, Iterable

from discord import app_commands


AGK_GUILD_ID = "1541131439599386644"
DEFAULT_BINDINGS_PATH = Path(
    "/home/private/workspace/personal-os/registry/channel-runtime-bindings.json"
)
PRIVATE_HERMES_HOME = Path("/home/private/.hermes")
PRIVATE_PROFILES_ROOT = PRIVATE_HERMES_HOME / "profiles"
_SAFE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SNOWFLAKE = re.compile(r"^[0-9]{15,22}$")
_CANONICAL_OS_IDS = frozenset({
    "alignment-os", "decision-os", "goal-life-strategy-os", "habit-tracker-os",
    "health-energy-os", "identity-shift-os", "intuitive-os", "journal-os",
    "mentor-os", "mindset-os", "nutrition-os", "oto100m-os",
    "social-intelligence-os",
})


@dataclass(frozen=True)
class OsConnectionBinding:
    os_id: str
    channel_id: str
    profile_id: str
    session_id: str
    session_key: str
    state_scope: str
    skill: str
    agent: str


def is_private_personal_os_home(hermes_home: Path) -> bool:
    """Return whether one Hermes home owns the PRIVATE Personal OS surface."""
    home = Path(hermes_home).resolve()
    return home == PRIVATE_HERMES_HOME or home.parent == PRIVATE_PROFILES_ROOT


def station_ui_command_names(hermes_home: Path) -> set[str]:
    """Return the dynamic slash surfaces preserved in UI-only mode."""
    home = Path(hermes_home).resolve()
    names = {"panel", "settings", "clear", "voice", "restart", "leave"}
    if home == Path("/home/operator/.hermes"):
        names.add("station-sessions")
    if is_private_personal_os_home(home):
        names.update({"os", "reset", "personal"})
    if home == PRIVATE_PROFILES_ROOT / "nutrition-os":
        names.update({"nutrition", "food"})
    return names


def _binding_from_row(os_id: str, row: object) -> OsConnectionBinding:
    if not _SAFE_ID.fullmatch(os_id) or os_id not in _CANONICAL_OS_IDS:
        raise ValueError("invalid canonical Personal OS identity")
    if not isinstance(row, dict):
        raise ValueError("invalid Personal OS binding")
    channel_id = str(row.get("channel_id") or "")
    session_id = str(row.get("session_id") or "")
    session_key = str(row.get("session_key") or "")
    state_scope = str(row.get("state_scope") or "")
    skill = str(row.get("skill") or "")
    agent = str(row.get("agent") or "")
    if not _SNOWFLAKE.fullmatch(channel_id):
        raise ValueError("invalid Personal OS channel binding")
    if not session_id or not session_key or not state_scope or not skill or not agent:
        raise ValueError("incomplete Personal OS runtime binding")
    if f"discord:group:{channel_id}:" not in session_key:
        raise ValueError("Personal OS session does not match its Discord channel")
    if not state_scope.startswith(f"os/{os_id}/sessions/"):
        raise ValueError("Personal OS state scope does not match its identity")
    return OsConnectionBinding(
        os_id=os_id,
        channel_id=channel_id,
        profile_id=os_id,
        session_id=session_id,
        session_key=session_key,
        state_scope=state_scope,
        skill=skill,
        agent=agent,
    )


def load_private_os_bindings(
    path: Path = DEFAULT_BINDINGS_PATH,
) -> dict[str, OsConnectionBinding]:
    """Load the immutable channel→OS connection map from PRIVATE state."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Personal OS connection registry is unavailable") from exc
    rows = payload.get("os_channels") if isinstance(payload, dict) else None
    if not isinstance(rows, dict) or set(rows) != _CANONICAL_OS_IDS:
        raise ValueError("Personal OS registry must contain exactly 13 canonical OS bindings")
    bindings: dict[str, OsConnectionBinding] = {}
    for os_id, raw in rows.items():
        binding = _binding_from_row(str(os_id), raw)
        if binding.channel_id in bindings:
            raise ValueError("duplicate Personal OS channel binding")
        bindings[binding.channel_id] = binding
    return bindings


def resolve_private_os_binding(
    channel_id: object,
    *,
    parent_channel_id: object = None,
    path: Path = DEFAULT_BINDINGS_PATH,
) -> OsConnectionBinding | None:
    """Resolve a canonical channel or a thread under that canonical channel."""
    bindings = load_private_os_bindings(path)
    direct = str(channel_id or "")
    parent = str(parent_channel_id or "")
    return bindings.get(direct) or bindings.get(parent)


def records_from_private_catalog(*_args, **_kwargs) -> tuple[OsConnectionBinding, ...]:
    """Compatibility shim for adapters upgraded from the obsolete control UI."""
    return tuple(load_private_os_bindings().values())


def records_from_snapshot(*_args, **_kwargs) -> tuple[()]:
    """The cross-Station snapshot is never authoritative for PRIVATE `/os`."""
    return tuple()


def _owner_ids_from_bindings(path: Path) -> set[int]:
    """Derive the canonical owner from immutable session keys, fail closed."""
    owners: set[int] = set()
    for binding in load_private_os_bindings(path).values():
        candidate = binding.session_key.rsplit(":", 1)[-1]
        if not candidate.isdigit():
            raise ValueError("invalid Personal OS owner binding")
        owners.add(int(candidate))
    if len(owners) != 1:
        raise ValueError("Personal OS bindings must share one canonical owner")
    return owners


def register_os_control_center(
    bot,
    catalog_loader: Callable[[], Iterable[object]] | None = None,
    *,
    owner_ids: set[int],
    bindings_path: Path = DEFAULT_BINDINGS_PATH,
) -> app_commands.Command:
    """Register `/os` as a read-only connection confirmation.

    The historical function name is retained only for adapter compatibility.
    The command has no view, modal, installer, doctor, action runner or state
    mutation. Existing canonical channel/session/state bindings remain the sole
    authority.
    """
    del catalog_loader
    authorized = {int(value) for value in owner_ids} | _owner_ids_from_bindings(bindings_path)

    async def command_callback(interaction):
        actor_id = int(getattr(getattr(interaction, "user", None), "id", 0) or 0)
        guild_id = int(getattr(interaction, "guild_id", 0) or 0)
        if actor_id not in authorized or guild_id != int(AGK_GUILD_ID):
            await interaction.response.send_message("Not authorized", ephemeral=True)
            return
        channel = getattr(interaction, "channel", None)
        binding = resolve_private_os_binding(
            getattr(interaction, "channel_id", None),
            parent_channel_id=getattr(channel, "parent_id", None),
            path=bindings_path,
        )
        if binding is None:
            await interaction.response.send_message(
                "`/os` is available only in a registered Personal OS channel.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Connected · `{binding.os_id}` · session `{binding.session_id}` · skill `{binding.skill}`",
            ephemeral=True,
        )

    command = app_commands.Command(
        name="os",
        description="Connect this Discord channel to its registered Personal OS.",
        callback=command_callback,
    )
    bot.tree.add_command(command, override=True)
    return command
