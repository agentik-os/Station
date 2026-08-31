# Station operations

## Health

```bash
station doctor
station status
```

## Discord

```bash
sudo station discord token list
sudo station discord token check TARGET
sudo station discord token rotate TARGET
```

Rotation uses hidden input and validates the Discord bot before writing. Application creation, privileged intents and OAuth installation remain owner-controlled.

## Completion Recovery

```bash
station recovery audit
sudo station recovery baseline
station recovery report
sudo station recovery decide FINDING RELAUNCH --actor gareth --source discord
sudo station recovery approve PROFILE MISSION REQUIREMENT --actor gareth --source discord --scope production
sudo station recovery oracle-pass PROFILE MISSION REPORT.json --actor completion-oracle
```

The auditor runs daily inside each profile boundary. Operator’s `/recap` command refreshes the current conversation audit, compares every archived prompt against requirements/artifacts/evidence, shows unfinished work and validates the Graph/Loop/Gauntlet/Oracle installation. `Relaunch Missing` is a staged owner action bound to that exact session and ledger; duplicate same-ledger relaunches are rejected. Root-owned approval files bind to the exact requirement digest. Root-owned Oracle verdicts bind to the complete ledger digest, including verified prompt archive bytes; any later prompt, requirement, artifact or evidence mutation invalidates stale trust. Discovery never authorizes execution.

## Backup and recovery

```bash
sudo station backup
sudo station backup --full-state
sudo station rollback
```

Recovery archives are local secrets. Keep them encrypted/off-host and never attach them to Discord or GitHub.

## Durability governance

Station ships a read-only durability controller. Audits emit only metadata,
counts and hashes: they never include profile-private contents, memory entry
text, cron prompts or credentials, and they never delete or rewrite state.

```bash
station durability profile-audit \
  --profiles-root "$HOME/.hermes/profiles" \
  --policy /usr/local/lib/station/overlay/config/station-durability-policy.json \
  --output "$HOME/.hermes/reports/durability/profiles.json"

station durability memory-audit \
  --memory-file "$HOME/.hermes/memories/MEMORY.md" \
  --output "$HOME/.hermes/reports/durability/memory.json"

# Export the current cron tool response to a local JSON file first; the audit
# reads that export and never patches Hermes' jobs store.
station durability cron-audit \
  --jobs-json /tmp/hermes-cron-export.json \
  --output "$HOME/.hermes/reports/durability/cron.json"

station durability fresh-session-gate RECEIPT.json

station durability isolated-pilot \
  --input-dir /tmp/station-pilot/input \
  --output-dir /tmp/station-pilot/output \
  --sudo --execute

station durability checkpoint-command \
  --repo /path/to/worktree --query-file /path/to/task.md
```

Permanent profiles require a separate durable identity, configuration or
credentials, memory or sessions, schedules or lifecycle, and a canonical
owner. Unknown and ownership-review profiles remain in place pending review.
Cron retirement and memory curation are proposal-only. Development sessions
opt into `--checkpoints --worktree`; checkpoints remain globally disabled so
runtime/system rollback continues to use explicit backups and manifests.

Before enabling an OS release or recurring automation, validate a fresh-session
receipt that binds project-context, Skills, toolsets, artifact, deterministic
checks, delivery and rollback.

## Updating

```bash
sudo station update --ref main
```

Production deployments should use a Station tag or 40-character commit. Update AGK-TUI/Hermes pins only after CI and a clean-host install test.

## Troubleshooting

1. `station doctor`
2. inspect the exact service, not every gateway;
3. preserve profile isolation and existing sessions;
4. restart only the affected unit;
5. verify the real Discord/Portal flow;
6. roll back if the release gate fails.
