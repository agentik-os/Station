# Station

**Station** is the portable AGK platform: **Discord + Hermes Agent + AGK-TUI + private Portal** on one Linux VPS.

Station turns a fresh Debian/Ubuntu server into four isolated AI workspaces:

- **Operator** — infrastructure and redacted global operations;
- **Agentik** — organisation, products and specialist workforce;
- **Mission** — clients and missions;
- **Private** — personal work and private agents.

## Product boundary

| Component | Owns |
|---|---|
| **Station** | Complete installer, releases, Portal, Discord lifecycle, policies, Kanban, Operative Systems, doctor, backup and rollback |
| **AGK-TUI** | RMUX mapping, durable terminal sessions and provider terminal UX |
| **Hermes Agent** | Agent runtime, profiles, tools, memory, providers and messaging gateways |
| **Discord** | Owner-created applications, bot authorization and human control surfaces |

Fix RMUX/TUI mapping bugs in [AGK-TUI](https://github.com/agentik-os/AGK-TUI). Fix the complete platform in Station.

## Install a fresh VPS

Inspect first:

```bash
curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/agentik-os/Station/main/install \
  | sudo bash -s -- --dry-run
```

Install:

```bash
curl --proto '=https' --tlsv1.2 -fsSL \
  https://raw.githubusercontent.com/agentik-os/Station/main/install \
  | sudo bash
```

Pin any Station tag or commit with `--ref REF`. Upstream AGK-TUI, Hermes and RMUX versions are independently pinned in `config/versions.lock`.

## First commands

```bash
station doctor
station status
station portal
station tui
```

Then authenticate providers independently for only the profiles that need them. Station never copies refreshable OAuth credentials between Linux homes.

## Discord setup and token rotation

Discord applications and OAuth authorization remain owner-controlled. Create/authorize each application in the Discord Developer Portal, enable its required intents, then rotate the corresponding token locally through hidden input:

```bash
sudo station discord token list
sudo station discord token rotate operator
sudo station discord token rotate agentik
sudo station discord token rotate mission
sudo station discord token rotate private
sudo station discord token rotate collective
sudo station discord token rotate nutrition-os
```

The command validates the new bot identity before writing, updates only the owning profile’s mode-0600 secret store, restarts only the exact gateway, verifies it is active and rolls back automatically if restart fails. Tokens are never accepted as command arguments or printed.

Read-only check:

```bash
sudo station discord token check operator
```

## Portal

`station portal` prints the current Tailscale MagicDNS URL. The Portal provides:

- compact Overview;
- station-scoped Kanban;
- Operative Systems;
- specialist Agents;
- redacted Sessions;
- the native Hermes dashboard.

Operator sees all four boundaries as sanitized operational metadata. Station URLs stay focused on their own organisation; prompt, message, secret, filesystem and client/profile identifiers are never published by the Fleet snapshot API.

## Operative Systems

Station installs and assigns:

- Research OS;
- Strategy OS;
- Builder OS;
- Evaluation OS;
- Nutrition OS when included by the installed release.

Their skills are projected into default and named Hermes profiles, while the package registry and assignments remain versioned and inspectable.

## Completion Recovery System

Station preserves original prompts inside each profile boundary, maps requirements to missions, artifacts and evidence, and rejects false `DONE` states. Significant missions use:

```text
PROMPT → REQUIREMENT GRAPH → TASKS → ARTIFACTS → EVIDENCE
       → VERIFICATION → GAUNTLET → COMPLETION ORACLE → DONE
```

Daily profile-local auditing finds incomplete, partial, falsely completed and promised-but-missing work. Operator receives only sanitized fleet metadata; original prompts remain in the owning Linux/Hermes profile and are loaded there only after an explicit human `RELAUNCH` decision.

```bash
station recovery audit
sudo station recovery baseline
station recovery report
sudo station recovery decide FINDING-ID RELAUNCH --actor gareth --source discord
sudo station recovery oracle-pass operator MISS-ID /home/operator/.hermes/reports/completion-oracle/MISS-ID.json --actor completion-oracle
```

Discord exposes `/recap` on Operator to audit the current conversation against every archived prompt, requirement, artifact, evidence, Gauntlet and Completion Oracle gate. Its `Relaunch Missing` button creates an explicit owner authorization and reinjects the recovery instruction into the same Discord/Hermes session. `/station-recovery` remains the fleet recovery center with `Keep Backlog`, `Already Done`, `Ignore`, Refresh and Close. Viewing either report never authorizes execution. Human approvals are root-owned and bound to the exact requirement digest; Completion Oracle verdicts are root-owned and bound to the exact full-ledger digest, so later edits invalidate stale trust automatically.

## Lifecycle

```bash
sudo station backup
sudo station backup --full-state
sudo station update
sudo station update --ref v0.2.0
sudo station rollback
```

Backups are local, mode 0600 and may contain credentials. Never publish them.

## Security model

- four separate Linux users and Hermes homes;
- loopback-only dashboards behind Tailscale Serve;
- profile-local Discord tokens and provider credentials;
- root-owned Fleet collector with a mode-0640 snapshot under a mode-0750 directory;
- sanitized Fleet API — no messages, prompts, paths, client IDs, raw profile IDs or secrets;
- Discord bot creation and OAuth stay with the owner;
- Hermes’ non-bypassable catastrophic-command blocklist remains active.

See [Architecture](docs/ARCHITECTURE.md) and [Operations](docs/OPERATIONS.md).
