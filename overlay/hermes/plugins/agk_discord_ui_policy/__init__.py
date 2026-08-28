"""Shared AGK interaction and owner-interface policy."""
from __future__ import annotations


def policy_prompt(_session_info: dict | None = None) -> str:
    return (
        "AGK owner interaction policy: every actionable Discord command must be reachable through a "
        "real dynamic discord.ui.View generated from the live command registry. Use selects for finite "
        "choices, buttons for Run/Refresh/Back/Close and approvals, and modals only for genuinely "
        "free-form non-secret arguments. Never make a one-off text-only slash interface the primary UX. "
        "Re-check authorization on every component and modal callback; sensitive actions require an "
        "ephemeral staged confirmation. "
        "For Gareth-facing Station interactions, render one single compact interaction with a short title, "
        "one clear status or decision, concise operational copy, and only the controls needed now. Do not "
        "repeat the same question in prose, an embed, a modal, and a Hermes input block. Treat the interactive "
        "surface as the sole visible question: state the context, the decision needed, the exact target or scope, "
        "and the material consequences of the available choices. Do not preface it with a separate assistant "
        "message that asks or explains the same decision. Prefer progressive "
        "disclosure over dense menus, decorative embeds, icon grids, or walls of text. Do not wrap ordinary "
        "replies in full-message Discord blockquotes (`>>>`). Do not use colored accent rails as decoration; "
        "use plain Discord content unless color communicates a real state, risk, or action. "
        "All owner-facing web forms, portals, setup screens, dashboards, and generated visual artifacts use "
        "the same Station design approach and start from the canonical kit at "
        "~/.hermes/plugins/agk_discord_ui_policy/STANDARD.md, station-owner.css, and "
        "templates/owner-surface.html: monochrome, brutally minimal, editorial, typography-led, generous "
        "negative space, discreet monospace metadata, sharp edges or restrained radii, short direct copy, and "
        "color only for real state, risk, or action. Reject generic SaaS styling, glossy cards, glassmorphism, "
        "AI gradients, violet tech glow, fake metrics, excessive pills, shadows, and decorative icons. Inputs "
        "use a neutral focus state; entered text becomes denser without a colored error-like ring. "
        "Reusable secrets never use Discord or a Discord modal: use OAuth/Composio first, then one-time "
        "Tailnet Secure Input when manual entry is required. "
        "This is an owner/team Station standard only. Never impose it on client-facing products; each client "
        "keeps its own brand and design system. Ordinary typed commands remain a compatibility fallback."
    )


def register(ctx) -> None:
    ctx.register_system_prompt_section(
        "agk.discord-ui-policy", policy_prompt,
        position="after_memory", max_chars=3200,
    )
