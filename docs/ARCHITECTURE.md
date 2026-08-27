# Station architecture

```text
Station installer / release lifecycle
├── AGK-TUI (pinned) ── RMUX + terminal/provider mapping
├── Hermes (pinned) ─── agents, profiles, tools, providers, gateways
├── Station overlay
│   ├── Fleet Portal
│   ├── Discord lifecycle
│   ├── Kanban station boards
│   ├── Operative System packages + skills
│   ├── global policy and capability stack
│   └── doctor / backup / update / rollback
└── Tailscale Serve ─── private HTTPS entrypoint
```

## State boundaries

Station shares code, never mutable identity state. Operator, Agentik, Mission and Private each retain independent Linux homes, Hermes databases, memories, sessions, provider credentials, Discord tokens, Composio state and RMUX sockets.

The root Fleet collector reads only allow-listed operational metadata and writes one group-restricted snapshot. The Fleet API applies a second redaction layer before returning any organisation view.

## Source ownership

- AGK-TUI bugs affecting RMUX, pane mapping, session opening or the terminal UI are fixed upstream and Station updates its immutable pin.
- Hermes runtime bugs are fixed upstream or in a narrowly scoped Station plugin/overlay until upstreamed.
- Fleet, Discord setup, OS packaging, lifecycle and full-VPS integration are Station code.

## Install graph

`Station source → pinned AGK-TUI → Station overlay → AGK bootstrap → pinned Hermes → four profiles → Kanban/OS/Discord → Fleet Portal → doctor`
