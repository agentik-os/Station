# Station Owner Interface Standard

Status: canonical
Scope: Operator, Agentik, Mission, Private, Collective, owner-facing AGK tools, and internal team utilities.
Exclusion: client-facing products retain their own brand and design system.

## Surface-first

Name the surface before designing it: Monitor, Operate, Configure, Command/Inspect, Decide/Learn, Explore, or Compare. The composition follows the job. Operational tools never masquerade as marketing pages.

## Visual direction

Station interfaces for Gareth are monochrome, brutally minimal, editorial, spacious, and typography-led. Use intentional negative space, a precise grotesk/sans hierarchy, discreet monospace metadata, full-width structural rules, sharp edges, and restrained radii. Color appears only for a real state, risk, or action.

Do not default to glossy cards, glassmorphism, AI/crypto gradients, violet tech glow, generic SaaS dashboards, centered hero-and-card compositions, fake metrics, decorative icon grids, excessive pills, shadows, or rounded rectangles used as hierarchy.

## Discord interaction contract

- One compact interaction per decision or operation.
- Short title, one clear status/decision, concise operational copy.
- Show only controls needed now; reveal advanced controls progressively.
- Selects for finite choices; buttons for Run, Refresh, Back, Close, approve, and cancel.
- Modals only for genuinely free-form, non-secret arguments.
- Never repeat the same question in prose, an embed, a modal, and native Hermes input UI.
- Do not wrap ordinary replies in full-message Discord blockquotes (`>>>`).
- Do not use colored accent rails as decoration; reserve color for a real state, risk, or action.
- Re-check authorization on every component and modal callback.
- Sensitive actions use ephemeral staged confirmation.
- Typed commands are a compatibility fallback, not the primary UX.

Discord native colors and component chrome cannot be fully themed. Consistency comes from information architecture, naming, brevity, ordering, progressive disclosure, and predictable controls—not decorative emoji or embed color.

## Web interaction contract

Use `station-owner.css` and `templates/owner-surface.html` as the default starting point for owner-facing Station web forms, portals, setup screens, and generated visual artifacts.

- Inputs are calm and structurally neutral.
- Focus uses caret, text density, and a subtle neutral line change—never a colored ring unless it represents a real state.
- Entered text becomes darker and medium/semibold.
- Token fields use visual masking plus explicit Show/Hide where practical.
- Copy is short, direct, and operational.
- Metadata, IDs, timestamps, and commands use monospace discreetly.
- Responsive layouts preserve hierarchy and whitespace rather than collapsing into card stacks.

## Tailnet Secure Input

Reusable secrets use OAuth/Composio first. When manual entry is unavoidable, use one-time Tailnet Secure Input—never Discord, a Discord modal, chat, email, issue trackers, or CLI arguments. The route is tailnet-only, random, CSRF-protected, no-store, size-limited, attempt-limited, expiring, no-log, and self-destructing. The credential passes in memory/stdin to the installer and is never echoed, hashed, logged, or returned.

## Client exclusion

This standard is for Gareth and the internal Station/team. Never impose it on client-facing products. Every client retains its own identity, typography, palette, product context, and design language. Security and accessibility requirements still apply.

## Release checklist

- Surface type is named and the composition matches it.
- Hierarchy comes from typography, space, alignment, and contrast before containers or color.
- Interaction is compact and does not duplicate questions.
- Refresh, Back, Close, and approval/cancel exist where relevant.
- Authorization is checked at every callback.
- Focus is neutral; state color is truthful.
- No generic SaaS/AI visual defaults or fake metrics.
- Secret handling follows Tailnet Secure Input.
- Client exclusion is respected.
