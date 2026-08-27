"""AGK capability routing and Agency Agents specialist tool."""

from __future__ import annotations

import os

from .agency_registry import AGENCY_TOOL_SCHEMA, agency_available, handle_agency

# Hugging Face's Xet transport can stall on IPv6-heavy headless VPS networks.
# Standard HTTPS is deterministic and respects an operator-provided override.
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

PROACTIVE_RULE = (
    "AGK proactive capability policy: Before every non-trivial task, inspect the live skills, "
    "plugins, tools, MCPs, specialist roster, and installed extensions that could improve "
    "correctness, speed, or quality. Load and use the best matching capabilities without waiting "
    "for the owner to name them. Search Agency Agents and delegate a bounded specialist when domain "
    "expertise materially helps. User instructions, authorization, secrets, profile isolation, "
    "approvals, reversibility, and verification outrank extension defaults. Never create uncontrolled "
    "loops or claim an agent was launched unless its real runtime or delegated task is verified."
)


def register(ctx) -> None:
    ctx.register_tool(
        name="agency_specialist",
        toolset="delegation",
        schema=AGENCY_TOOL_SCHEMA,
        handler=handle_agency,
        check_fn=agency_available,
        description="Search and load pinned Agency Agents specialist briefs for proactive task routing.",
        emoji="🎭",
    )
    ctx.register_system_prompt_section(
        "agk.power-stack",
        lambda _session_info=None: PROACTIVE_RULE,
        position="after_memory",
        max_chars=1400,
    )
