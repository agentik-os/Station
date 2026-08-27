# DISCORD INTELLIGENT TEAM CONTROL PLANE

## Purpose

Discord is the human-facing AGK control plane. It should expose mission state, evidence, approvals, recovery findings and actions without forcing the CTO to chase agents or reconstruct context.

## #general — Daily Recovery Card

After the daily audit, post one compact summary:

```text
AGK DAILY COMPLETION AUDIT — 2026-08-27

Prompts reviewed: 124
Missions checked: 18
✅ Verified: 13
⚠ Partial: 3
❌ Incomplete: 2
🧑 Human decisions: 1

Report: AGK_DAILY_COMPLETION_AUDIT.md
Operator package: AGK_OPERATOR_RECOVERY.md
```

Recommended buttons:

```text
[ 🔥 REVIEW MISSING WORK ]
[ 📄 OPEN REPORT ]
[ 🧠 SEND TO OPERATOR ]
```

## Mission recovery card

For each important incomplete mission:

```text
AGK RECOVERY — MISSION MISS-142

Project: AGK
Source: Operator prompt / 2026-08-26
Status: PARTIALLY COMPLETE
Severity: P1

Requested: 12 requirements
Verified: 9
Missing: 3

Missing:
- Discord validation button
- persistent handoff object
- post-deploy evidence mapping

Recommended route:
operator → agentik/works

[ ▶ RELAUNCH MISSION ]
[ 📄 VIEW FULL PROMPT ]
[ 📎 VIEW EVIDENCE ]
[ 🗂 KEEP BACKLOG ]
[ ✅ ALREADY DONE ]
[ ❌ IGNORE ]
```

## RELAUNCH MISSION behavior

`RELAUNCH MISSION` must NOT blindly repeat the original mission.

It must:

```text
1. Record the human authorization event.
2. Load the original prompt and Requirement Graph.
3. Load verified completed work and evidence.
4. Load the missing/unverified requirement nodes.
5. Create or reopen the canonical AGK mission.
6. Send the recovery package to Operator.
7. Operator selects the appropriate session/team.
8. Execute ONLY missing/reopened nodes unless dependencies require broader work.
9. Run Gauntlet + Verification Engineering.
10. Return evidence and updated graph to Discord/Linear.
```

Never duplicate already verified work.

## Full prompt inside mission

Every relaunched mission must contain the original context required to understand the request, either embedded directly or by stable canonical references.

Mission context package:

```text
Original user prompt
Relevant previous prompts
Requirement Graph
Acceptance criteria
Known constraints
Completed work
Remaining work
Artifacts/evidence
Relevant files/commits/PRs
Human authorization
Recommended route
Definition of Done
```

This prevents the receiving agent from losing context or asking the CTO to explain the mission again.

## Mission thread

When relaunched, create/use a dedicated Discord mission thread containing:

```text
Mission header
Linear issue
Original request
Requirement ledger
Assigned team/session
Live status
PR/staging links
Evidence
Gauntlet results
Human approval controls
Final verification
```

## Human control rule

Buttons that create work must represent an explicit human action.

Never interpret merely opening a report, viewing evidence, or reading a backlog item as authorization to execute.

Execution-triggering controls include:

```text
START MISSION
RELAUNCH MISSION
APPROVE RECOVERY
APPROVE PROD
```

## Completion return card

After recovered work is finished:

```text
MISSION RECOVERY COMPLETE — MISS-142

Recovered requirements: 3/3
Gauntlet: PASS
Verification: PASS
Evidence: attached
Linear: updated

[ 🔎 REVIEW EVIDENCE ]
[ ✅ ACCEPT ]
[ ❌ REQUEST CHANGES ]
```
