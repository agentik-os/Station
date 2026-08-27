# AGK IMPLEMENTATION BLUEPRINT

## Core services

Recommended logical components:

```text
Prompt Archive
Requirement Extractor
Requirement Graph Store
Mission/Task Graph
Execution Harness
Artifact Registry
Evidence Registry
Verification Engine
Gauntlet Runner
Completeness Oracle
Recovery Auditor
Operator Router
Discord Control Plane
Linear Adapter
Git/GitHub Adapter
Session Registry
Event/Audit Log
```

## Canonical event examples

```text
prompt.archived
requirements.extracted
requirement.created
mission.created
mission.authorized
mission.relaunched
task.started
artifact.created
verification.failed
verification.passed
gauntlet.failed
gauntlet.passed
completeness.failed
completeness.passed
recovery.finding.created
recovery.approved
operator.dispatched
mission.completed
```

## Suggested data objects

### PromptRecord
- id
- timestamp
- source
- session
- client/project
- original_content
- references

### Requirement
- id
- prompt_id
- text
- type
- status
- provenance
- dependencies
- acceptance_criteria
- evidence_ids

### Mission
- id
- client/project
- source_prompt_ids
- requirement_ids
- authorization
- state
- assigned_session/team

### Artifact
- id
- type
- location
- creator_run
- related_requirement_ids

### Evidence
- id
- verifier
- result
- artifact/reference
- related_requirement_ids

### RecoveryFinding
- id
- classification
- severity
- source_prompt_ids
- missing_requirement_ids
- recommended_route
- human_decision

## Cron / schedule

Suggested default:

```text
Daily Recovery Auditor: once per day
Immediate Completeness Oracle: every significant mission before DONE
Gauntlet: every significant code/release mission before human approval or DONE
Historical Baseline: one-time initial run, then manually re-runnable
```

## Important invariant

The daily Recovery Auditor discovers and reports. It does not auto-start backlog work. The live mission Harness may continue already-authorized work until its approved graph is closed, but it may not expand into newly discovered backlog scope without human authorization.
