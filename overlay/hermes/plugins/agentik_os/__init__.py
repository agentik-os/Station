"""Agentik OS business command layer.

Hermes remains the runtime. This plugin owns the persistent business objects
and their command grammar without adding model-tool schema to the core.
"""

from __future__ import annotations

from .commands import AgentikCommandService
from .nutrition_command import dispatch as dispatch_nutrition
from .runtime_tool import RUNTIME_TOOL_SCHEMA, handle_runtime, runtime_available
from .agent_registry import AGENT_TOOL_SCHEMA, AgentCommandService, agent_router_prompt, handle_agent
from .owner_context import owner_context_prompt
from .interagent import INTERAGENT_TOOL_SCHEMA, broker_available, handle_interagent, interagent_prompt
from .rules import rules_prompt
from .completion import (
    AGK_COMPLETION_TOOL_SCHEMA,
    archive_before_execution,
    completion_available,
    completion_prompt,
    handle_completion,
)


def register(ctx) -> None:
    service = AgentikCommandService.from_runtime()
    for name in service.command_names:
        ctx.register_command(
            name,
            handler=service.handler(name),
            description=service.description(name),
            args_hint="<action> [target] [options]",
        )
    ctx.register_command(
        "nutrition",
        handler=dispatch_nutrition,
        description="Operate the active Nutrition OS cycle.",
        args_hint="status|plan|shop|prep|next|audit|reset [options]",
    )
    # Backward-compatible alias for existing automations and conversations.
    ctx.register_command(
        "food",
        handler=dispatch_nutrition,
        description="Compatibility alias for the active Nutrition OS cycle.",
        args_hint="status|plan|shop|prep|next|audit|reset [options]",
    )
    ctx.register_tool(
        name="agentik_runtime",
        toolset="terminal",
        schema=RUNTIME_TOOL_SCHEMA,
        handler=handle_runtime,
        check_fn=runtime_available,
        description="Persistent per-user Hermes, Claude and Codex orchestration through AGK/RMUX.",
        emoji="🧭",
    )
    agent_service = AgentCommandService()
    ctx.register_command(
        "agent",
        handler=agent_service.dispatch,
        description="List and operate specialized agents through durable AGK/RMUX sessions.",
        args_hint="list|start|status|message|logs <agent> [instruction]",
    )
    ctx.register_tool(
        name="agentik_agent",
        toolset="terminal",
        schema=AGENT_TOOL_SCHEMA,
        handler=handle_agent,
        check_fn=runtime_available,
        description="Specialized Hermes agents backed by persistent AGK/RMUX runtimes.",
        emoji="🤖",
    )
    ctx.register_tool(
        name="station_interagent",
        toolset="terminal",
        schema=INTERAGENT_TOOL_SCHEMA,
        handler=handle_interagent,
        check_fn=broker_available,
        description="UID-authenticated non-secret team messaging across Station agents, administered by Operator.",
        emoji="↔",
    )
    ctx.register_tool(
        name="agk_completion",
        toolset="terminal",
        schema=AGK_COMPLETION_TOOL_SCHEMA,
        handler=handle_completion,
        check_fn=completion_available,
        description="Persistent prompt, requirement, artifact, evidence and completion-gate graph.",
        emoji="✓",
    )
    ctx.register_hook("pre_llm_call", archive_before_execution)
    ctx.register_system_prompt_section(
        "agentik.agent-router",
        agent_router_prompt,
        position="after_memory",
        max_chars=1400,
    )
    ctx.register_system_prompt_section(
        "agentik.completion-harness",
        completion_prompt,
        position="after_memory",
        max_chars=1800,
    )
    ctx.register_system_prompt_section(
        "agentik.global-rules",
        rules_prompt,
        position="after_memory",
        max_chars=3999,
    )
    ctx.register_system_prompt_section(
        "agentik.owner-context",
        owner_context_prompt,
        position="after_memory",
        max_chars=3999,
    )
    ctx.register_system_prompt_section(
        "agentik.interagent",
        interagent_prompt,
        position="after_memory",
        max_chars=1200,
    )
