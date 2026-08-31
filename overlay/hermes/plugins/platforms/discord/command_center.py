"""Registry-driven Discord command center.

The panel reads Hermes' existing built-in and plugin command registries on every
refresh.  It deliberately owns no command registry of its own: UI values resolve
through immutable :class:`CommandTarget` snapshots kept server-side by the view.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

import discord

COMMANDS_PER_PAGE = 25
_CUSTOM_ID_PREFIX = "hermes:panel"
_MAX_ARGUMENT_LENGTH = 1000
_SENSITIVE_ARGUMENT_PATTERN = re.compile(
    r"(?:api[-_ ]?key|api[-_ ]?token|access[-_ ]?token|refresh[-_ ]?token|"
    r"password|passwd|credential|private[-_ ]?key|client[-_ ]?secret|\bsecret\b)",
    re.IGNORECASE,
)


def _bounded(text: Any, limit: int) -> str:
    value = str(text or "").strip()
    return value if len(value) <= limit else value[: max(0, limit - 1)] + "…"


def _target_custom_id(name: str, plugin: str | None) -> str:
    identity = f"{plugin or 'builtin'}\0{name}".encode("utf-8")
    digest = hashlib.blake2s(identity, digest_size=8).hexdigest()
    return f"{_CUSTOM_ID_PREFIX}:cmd:{digest}"


def _finite_choices(args_hint: str, subcommands: Sequence[str] = ()) -> tuple[str, ...]:
    values = tuple(dict.fromkeys(str(value).strip() for value in subcommands if str(value).strip()))
    if values:
        return values[:COMMANDS_PER_PAGE]

    hint = str(args_hint or "").strip()
    match = re.fullmatch(r"[<\[]([^>\]]+)[>\]]", hint)
    if not match or "|" not in match.group(1):
        return ()
    candidates = tuple(part.strip() for part in match.group(1).split("|"))
    if not all(candidates) or len(candidates) > COMMANDS_PER_PAGE:
        return ()
    return tuple(dict.fromkeys(candidates))


@dataclass(frozen=True, slots=True)
class CommandTarget:
    """Immutable server-side snapshot of one executable registry command."""

    name: str
    description: str
    category: str
    args_hint: str = ""
    argument_choices: tuple[str, ...] = ()
    plugin: str | None = None
    custom_id: str = ""

    @classmethod
    def create(
        cls,
        *,
        name: str,
        description: str,
        category: str,
        args_hint: str = "",
        argument_choices: Sequence[str] = (),
        plugin: str | None = None,
    ) -> "CommandTarget":
        canonical_name = str(name).strip().lower().lstrip("/")
        if not canonical_name:
            raise ValueError("command name is required")
        canonical_plugin = str(plugin).strip() if plugin else None
        choices = tuple(
            dict.fromkeys(str(choice).strip() for choice in argument_choices if str(choice).strip())
        )[:COMMANDS_PER_PAGE]
        return cls(
            name=canonical_name,
            description=_bounded(description or f"Run /{canonical_name}", 100),
            category=_bounded(category or "Other", 100),
            args_hint=_bounded(args_hint, 100),
            argument_choices=choices,
            plugin=canonical_plugin,
            custom_id=_target_custom_id(canonical_name, canonical_plugin),
        )


def aggregate_command_targets(
    *,
    builtins: Iterable[Any] | None = None,
    plugins: Mapping[str, Mapping[str, Any]] | None = None,
    is_gateway_available: Callable[[Any], bool] | None = None,
) -> tuple[CommandTarget, ...]:
    """Return one deterministic snapshot from the live canonical registries."""
    if builtins is None:
        from hermes_cli.commands import COMMAND_REGISTRY, _is_gateway_available, _resolve_config_gates

        builtins = COMMAND_REGISTRY
        config_overrides = _resolve_config_gates()
        is_gateway_available = lambda command: _is_gateway_available(command, config_overrides)
    if plugins is None:
        from hermes_cli.plugins import get_plugin_commands

        plugins = get_plugin_commands()
    if is_gateway_available is None:
        is_gateway_available = lambda command: not bool(getattr(command, "cli_only", False))

    targets: list[CommandTarget] = []
    names: set[str] = set()
    for command in builtins:
        if not is_gateway_available(command):
            continue
        name = str(getattr(command, "name", "")).strip().lower()
        if not name or name in names:
            continue
        args_hint = str(getattr(command, "args_hint", "") or "")
        targets.append(
            CommandTarget.create(
                name=name,
                description=getattr(command, "description", ""),
                category=getattr(command, "category", "Other"),
                args_hint=args_hint,
                argument_choices=_finite_choices(
                    args_hint, getattr(command, "subcommands", ()) or ()
                ),
            )
        )
        names.add(name)

    for raw_name, entry in plugins.items():
        name = str(raw_name).strip().lower().lstrip("/")
        if not name or name in names or not isinstance(entry, Mapping):
            continue
        plugin = str(entry.get("plugin") or "Plugin").strip()
        args_hint = str(entry.get("args_hint") or "")
        targets.append(
            CommandTarget.create(
                name=name,
                description=str(entry.get("description") or "Plugin command"),
                category=f"Plugins · {plugin}",
                args_hint=args_hint,
                argument_choices=_finite_choices(args_hint),
                plugin=plugin,
            )
        )
        names.add(name)

    targets.sort(key=lambda item: (item.category.casefold(), item.name.casefold()))
    ids = [target.custom_id for target in targets]
    if len(ids) != len(set(ids)):
        raise ValueError("command target ID collision")
    return tuple(targets)


def paginate_commands(
    commands: Sequence[CommandTarget], page_size: int = COMMANDS_PER_PAGE
) -> tuple[tuple[CommandTarget, ...], ...]:
    if page_size < 1 or page_size > COMMANDS_PER_PAGE:
        raise ValueError("page_size must be between 1 and 25")
    ordered = tuple(commands)
    if not ordered:
        return ((),)
    return tuple(
        ordered[index : index + page_size]
        for index in range(0, len(ordered), page_size)
    )


def argument_kind(target: CommandTarget) -> str:
    if target.argument_choices:
        return "finite"
    sensitivity_probe = " ".join((target.args_hint, target.description, target.name))
    if target.args_hint and _SENSITIVE_ARGUMENT_PATTERN.search(sensitivity_probe):
        return "secure_handoff"
    if target.args_hint:
        return "text"
    return "none"


Authorize = Callable[[Any, CommandTarget | None], bool | Awaitable[bool]]
Execute = Callable[[Any, CommandTarget, str], Any | Awaitable[Any]]
CatalogFactory = Callable[[], Sequence[CommandTarget]]


class _CommandArgumentsModal(discord.ui.Modal):
    def __init__(self, owner: "CommandCenterView", target: CommandTarget):
        super().__init__(
            title=_bounded(f"/{target.name} arguments", 45),
            custom_id=f"{_CUSTOM_ID_PREFIX}:modal:{target.custom_id.rsplit(':', 1)[-1]}",
            timeout=300,
        )
        self.owner = owner
        self.target = target
        self.arguments = discord.ui.TextInput(
            label="Arguments",
            placeholder=_bounded(target.args_hint or "Enter command arguments", 100),
            required=True,
            max_length=_MAX_ARGUMENT_LENGTH,
            custom_id=f"{_CUSTOM_ID_PREFIX}:modal:arguments",
        )
        self.add_item(self.arguments)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await self.owner._authorized(interaction, self.target):
            return
        await self.owner._execute(interaction, self.target, str(self.arguments.value).strip())


class CommandCenterView(discord.ui.View):
    """Dynamic, authorized Discord UI over canonical Hermes commands."""

    def __init__(
        self,
        catalog_factory: CatalogFactory,
        *,
        authorize: Authorize,
        execute: Execute,
        timeout: float = 300,
    ) -> None:
        super().__init__(timeout=timeout)
        self._catalog_factory = catalog_factory
        self._authorize = authorize
        self._execute_callback = execute
        self.targets: tuple[CommandTarget, ...] = ()
        self._targets_by_id: dict[str, CommandTarget] = {}
        self.categories: tuple[str, ...] = ()
        self.level = "categories"
        self.category: str | None = None
        self.page = 0
        self.category_page = 0
        self.selected_target: CommandTarget | None = None
        self.refresh_catalog()
        self._render()

    @property
    def content(self) -> str:
        if self.level == "command" and self.selected_target is not None:
            target = self.selected_target
            argument = target.args_hint or "No arguments"
            source = f"Plugin: {target.plugin}\n" if target.plugin else ""
            secure_note = (
                "\nDiscord will not collect reusable secrets. Use the secure handoff below."
                if argument_kind(target) == "secure_handoff"
                else ""
            )
            return (
                f"**Command center · /{target.name}**\n"
                f"{target.description}\n{source}Arguments: `{argument}`{secure_note}"
            )
        if self.level == "commands" and self.category:
            commands = self._category_targets(self.category)
            pages = paginate_commands(commands)
            return (
                f"**Command center · {self.category}**\n"
                f"Choose a command. Page {self.page + 1}/{len(pages)}."
            )
        return "**Command center**\nChoose a category."

    def refresh_catalog(self) -> None:
        targets = tuple(self._catalog_factory())
        self.targets = targets
        self._targets_by_id = {target.custom_id: target for target in targets}
        self.categories = tuple(sorted({target.category for target in targets}, key=str.casefold))
        if self.category not in self.categories:
            self.category = None
            self.level = "categories"
            self.page = 0
        if self.selected_target and self.selected_target.custom_id not in self._targets_by_id:
            self.selected_target = None
            self.level = "commands" if self.category else "categories"

    def show_category(self, category: str) -> None:
        if category not in self.categories:
            raise KeyError("unknown command category")
        self.category = category
        self.level = "commands"
        self.page = 0
        self.selected_target = None
        self._render()

    def show_command(self, target_id: str) -> None:
        target = self._targets_by_id.get(target_id)
        if target is None:
            raise KeyError("unknown command target")
        self.category = target.category
        self.selected_target = target
        self.level = "command"
        self._render()

    def _category_targets(self, category: str) -> tuple[CommandTarget, ...]:
        return tuple(target for target in self.targets if target.category == category)

    async def _call(self, callback: Callable[..., Any], *args: Any) -> Any:
        result = callback(*args)
        return await result if inspect.isawaitable(result) else result

    async def _authorized(
        self, interaction: discord.Interaction, target: CommandTarget | None
    ) -> bool:
        allowed = bool(await self._call(self._authorize, interaction, target))
        if allowed:
            return True
        response = getattr(interaction, "response", None)
        if response is not None:
            await response.send_message(
                content="You are not authorized to use this command center action.",
                ephemeral=True,
            )
        return False

    async def _execute(
        self, interaction: discord.Interaction, target: CommandTarget, argument: str
    ) -> None:
        await self._call(self._execute_callback, interaction, target, argument)

    def _add_button(
        self,
        *,
        label: str,
        custom_id: str,
        callback: Callable[[discord.Interaction], Awaitable[None]],
        style: Any = discord.ButtonStyle.secondary,
        disabled: bool = False,
    ) -> None:
        button = discord.ui.Button(
            label=label,
            custom_id=custom_id,
            style=style,
            disabled=disabled,
        )
        button.callback = callback
        self.add_item(button)

    def _render(self) -> None:
        self.clear_items()
        if self.level == "categories":
            self._render_categories()
        elif self.level == "commands":
            self._render_commands()
        else:
            self._render_command()
        self._render_navigation()

    def _render_categories(self) -> None:
        pages = tuple(
            self.categories[index : index + COMMANDS_PER_PAGE]
            for index in range(0, len(self.categories), COMMANDS_PER_PAGE)
        ) or ((),)
        self.category_page = min(self.category_page, len(pages) - 1)
        options = [
            discord.SelectOption(
                label=_bounded(category, 100),
                value=category,
                description=f"{len(self._category_targets(category))} commands",
            )
            for category in pages[self.category_page]
        ]
        select = discord.ui.Select(
            placeholder="Choose a category",
            options=options,
            custom_id=f"{_CUSTOM_ID_PREFIX}:categories:{self.category_page}",
        )

        async def choose(interaction: discord.Interaction) -> None:
            if not await self._authorized(interaction, None):
                return
            values = tuple((getattr(interaction, "data", None) or {}).get("values", ()))
            category = values[0] if values else ""
            if category not in self.categories:
                await interaction.response.send_message(
                    content="That category is no longer available. Refresh the command center.",
                    ephemeral=True,
                )
                return
            self.show_category(category)
            await interaction.response.edit_message(content=self.content, view=self)

        select.callback = choose
        self.add_item(select)
        if len(pages) > 1:
            self._add_button(
                label="Previous",
                custom_id=f"{_CUSTOM_ID_PREFIX}:category-page:previous",
                callback=self._previous_category_page,
                disabled=self.category_page == 0,
            )
            self._add_button(
                label="Next",
                custom_id=f"{_CUSTOM_ID_PREFIX}:category-page:next",
                callback=self._next_category_page,
                disabled=self.category_page >= len(pages) - 1,
            )

    def _render_commands(self) -> None:
        commands = self._category_targets(self.category or "")
        pages = paginate_commands(commands)
        self.page = min(self.page, len(pages) - 1)
        options = [
            discord.SelectOption(
                label=_bounded(f"/{target.name}", 100),
                value=target.custom_id,
                description=_bounded(target.description, 100),
            )
            for target in pages[self.page]
        ]
        select = discord.ui.Select(
            placeholder="Choose a command",
            options=options,
            custom_id=f"{_CUSTOM_ID_PREFIX}:commands:{self.page}",
        )

        async def choose(interaction: discord.Interaction) -> None:
            values = tuple((getattr(interaction, "data", None) or {}).get("values", ()))
            target = self._targets_by_id.get(values[0] if values else "")
            if target is None:
                await interaction.response.send_message(
                    content="That command is no longer available. Refresh the command center.",
                    ephemeral=True,
                )
                return
            if not await self._authorized(interaction, target):
                return
            self.show_command(target.custom_id)
            await interaction.response.edit_message(content=self.content, view=self)

        select.callback = choose
        self.add_item(select)
        if len(pages) > 1:
            self._add_button(
                label="Previous",
                custom_id=f"{_CUSTOM_ID_PREFIX}:command-page:previous",
                callback=self._previous_command_page,
                disabled=self.page == 0,
            )
            self._add_button(
                label="Next",
                custom_id=f"{_CUSTOM_ID_PREFIX}:command-page:next",
                callback=self._next_command_page,
                disabled=self.page >= len(pages) - 1,
            )

    def _render_command(self) -> None:
        target = self.selected_target
        if target is None:
            return
        kind = argument_kind(target)
        if kind == "finite":
            options = [
                discord.SelectOption(label=_bounded(value, 100), value=value)
                for value in target.argument_choices
            ]
            select = discord.ui.Select(
                placeholder=_bounded(target.args_hint or "Choose an argument", 150),
                options=options,
                custom_id=f"{_CUSTOM_ID_PREFIX}:argument:{target.custom_id.rsplit(':', 1)[-1]}",
            )

            async def choose_argument(interaction: discord.Interaction) -> None:
                values = tuple((getattr(interaction, "data", None) or {}).get("values", ()))
                argument = values[0] if values else ""
                if argument not in target.argument_choices:
                    await interaction.response.send_message(
                        content="That argument is not available for this command.", ephemeral=True
                    )
                    return
                if not await self._authorized(interaction, target):
                    return
                await self._execute(interaction, target, argument)

            select.callback = choose_argument
            self.add_item(select)
        elif kind == "secure_handoff":
            async def secure_handoff(interaction: discord.Interaction) -> None:
                if not await self._authorized(interaction, target):
                    return
                await interaction.response.send_message(
                    content=(
                        "Use OAuth/Composio when the provider supports it. Otherwise open "
                        "Tailnet Secure Input for a one-time encrypted handoff. Do not paste "
                        "passwords, tokens, API keys, credentials, or private keys into Discord."
                    ),
                    ephemeral=True,
                )

            self._add_button(
                label="Secure handoff",
                custom_id=f"{_CUSTOM_ID_PREFIX}:secure:{target.custom_id.rsplit(':', 1)[-1]}",
                callback=secure_handoff,
                style=discord.ButtonStyle.primary,
            )
        elif kind == "text":
            async def open_modal(interaction: discord.Interaction) -> None:
                if not await self._authorized(interaction, target):
                    return
                await interaction.response.send_modal(_CommandArgumentsModal(self, target))

            self._add_button(
                label="Enter arguments",
                custom_id=f"{_CUSTOM_ID_PREFIX}:arguments:{target.custom_id.rsplit(':', 1)[-1]}",
                callback=open_modal,
                style=discord.ButtonStyle.primary,
            )
        else:
            async def run(interaction: discord.Interaction) -> None:
                if not await self._authorized(interaction, target):
                    return
                await self._execute(interaction, target, "")

            self._add_button(
                label="Run",
                custom_id=f"{_CUSTOM_ID_PREFIX}:run:{target.custom_id.rsplit(':', 1)[-1]}",
                callback=run,
                style=discord.ButtonStyle.primary,
            )

    def _render_navigation(self) -> None:
        if self.level != "categories":
            self._add_button(
                label="Back",
                custom_id=f"{_CUSTOM_ID_PREFIX}:back",
                callback=self._go_back,
            )
        self._add_button(
            label="Refresh",
            custom_id=f"{_CUSTOM_ID_PREFIX}:refresh",
            callback=self._refresh_panel,
        )
        self._add_button(
            label="Close",
            custom_id=f"{_CUSTOM_ID_PREFIX}:close",
            callback=self._close_panel,
        )

    async def _refresh_panel(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction, self.selected_target):
            return
        self.refresh_catalog()
        self._render()
        await interaction.response.edit_message(content=self.content, view=self)

    async def _go_back(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction, self.selected_target):
            return
        if self.level == "command" and self.category in self.categories:
            self.level = "commands"
            self.selected_target = None
        else:
            self.level = "categories"
            self.category = None
            self.page = 0
        self._render()
        await interaction.response.edit_message(content=self.content, view=self)

    async def _close_panel(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction, self.selected_target):
            return
        self.stop()
        await interaction.response.edit_message(content="Command center closed.", view=None)

    async def _previous_command_page(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction, None):
            return
        self.page = max(0, self.page - 1)
        self._render()
        await interaction.response.edit_message(content=self.content, view=self)

    async def _next_command_page(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction, None):
            return
        pages = paginate_commands(self._category_targets(self.category or ""))
        self.page = min(len(pages) - 1, self.page + 1)
        self._render()
        await interaction.response.edit_message(content=self.content, view=self)

    async def _previous_category_page(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction, None):
            return
        self.category_page = max(0, self.category_page - 1)
        self._render()
        await interaction.response.edit_message(content=self.content, view=self)

    async def _next_category_page(self, interaction: discord.Interaction) -> None:
        if not await self._authorized(interaction, None):
            return
        pages = max(1, (len(self.categories) + COMMANDS_PER_PAGE - 1) // COMMANDS_PER_PAGE)
        self.category_page = min(pages - 1, self.category_page + 1)
        self._render()
        await interaction.response.edit_message(content=self.content, view=self)
