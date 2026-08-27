# AGK COMPLETION & RECOVERY SYSTEM — GLOBAL MASTER

## Purpose

This policy applies to every AGK/Hermes agent and every execution domain. Its purpose is to eliminate silent incomplete work when prompts contain large amounts of context, multiple constraints, multiple deliverables, or work spanning several sessions.

The system must never trust an agent's local impression that a task is complete. Completion is decided by a persistent requirement graph, verification evidence, and an independent completeness audit.

## 1. Global completion law

Every meaningful prompt must follow:

```text
PROMPT
→ ARCHIVE ORIGINAL PROMPT
→ REQUIREMENT EXTRACTION
→ REQUIREMENT GRAPH
→ TASK GRAPH
→ EXECUTION
→ ARTIFACTS
→ VERIFICATION
→ GAUNTLET LOOP
→ COMPLETENESS ORACLE
→ LOOP-GRAPH UNTIL CLOSED
→ HUMAN APPROVAL WHEN REQUIRED
→ DONE
```

Never treat a long prompt as one instruction. Extract all explicit requirements, implicit necessary requirements, constraints, deliverables, acceptance criteria, dependencies, prohibited actions, approval gates and required evidence.

## 2. No silent drop

An agent may never silently omit work because of context length, token pressure, complexity, low perceived importance, uncertainty, missing dependencies, session switching, summarization or implementation difficulty.

Every requirement must end in exactly one state:

```text
PENDING
ACTIVE
DONE
VERIFIED
BLOCKED
HUMAN_REQUIRED
DEFERRED_BY_HUMAN
NOT_APPLICABLE
```

If an agent cannot complete something, it must record the reason explicitly. Silent omission is mission failure.

## 3. Requirement provenance

Each requirement must retain a reference to the original prompt and relevant source fragment.

```text
REQ-018
source_prompt_id: P-2026-08-27-142
source_session: operator
source_message: 47
requirement: Human approval required before backlog execution
status: VERIFIED
evidence:
  - workflow configuration
  - approval event
```

Compressed summaries are navigation aids only. They are never the sole source of truth when the original prompt is available.

## 4. Completion ledger

Every mission maintains a Requirement Ledger.

```text
REQ-001 VERIFIED
REQ-002 VERIFIED
REQ-003 PENDING
REQ-004 BLOCKED
REQ-005 VERIFIED
```

A mission cannot become DONE while any executable requirement is PENDING or ACTIVE.

## 5. Harness Engineering

AGK Harness owns completion state, not the individual agent.

The Harness provides and persists:

```text
Original context
Requirement graph
Task graph
Constraints
Permissions
Tools
Artifacts
Evidence
Verification results
Human approvals
Completion gate
```

Example:

```text
Agent: "Task complete."
AGK Harness: "Completion rejected. REQ-014 and REQ-021 unresolved. Continue."
```

## 6. Graph Engineering

Complex work must be represented as linked graphs:

```text
PROMPT
→ CONTEXT GRAPH
→ REQUIREMENT GRAPH
→ MISSION GRAPH
→ TASK GRAPH
→ EXECUTION GRAPH
→ ARTIFACT GRAPH
→ EVIDENCE GRAPH
```

Canonical traceability:

```text
Prompt → Requirement → Task → Agent Run → Artifact → Verification → Evidence
```

Any missing edge is a candidate completeness failure.

## 7. Loop-Graph Engineering

Execution continues until the graph is closed.

```text
while unresolved_requirements > 0:
    select unresolved node
    inspect original source
    inspect dependencies
    route to correct agent/session
    execute
    collect artifact
    verify
    update graph

run global completeness audit

if missing work detected:
    add/reopen requirement nodes
    continue loop
else:
    permit completion
```

Do not stop because one local agent finished its subtask.

## 8. Verification Engineering

Every significant mission must pass four verification layers:

1. Requirement Verification — did we do everything requested?
2. Implementation Verification — does the work actually function?
3. Evidence Verification — can the result be proven?
4. State Verification — does the mission status match reality?

## 9. Gauntlet Loop

```text
BUILD
→ VERIFY
→ CHALLENGE
→ SEARCH FOR MISSING WORK
→ FIX
→ RE-VERIFY
→ PASS OR LOOP
```

The Gauntlet validates both correctness and completeness.

The verifier must actively attempt to prove the mission incomplete:

```text
What requested item is missing?
What was partially implemented?
What original constraint was forgotten?
What deliverable has no evidence?
What source prompt statement has no matching task?
What task has no artifact?
What acceptance criterion has not been verified?
What dependency was ignored?
What promised follow-up was never executed?
```

If something is found, FAIL the completion gate and reopen the graph.

## 10. Completeness Oracle

Create an independent global agent: `@Completion Oracle`.

It never implements work. It compares:

```text
Original prompts
vs Requirement Graph
vs Tasks
vs Artifacts
vs Evidence
vs Mission State
```

Allowed output classifications:

```text
COMPLETE
PARTIAL
INCOMPLETE
BLOCKED
FALSELY_MARKED_DONE
PROMISED_NOT_COMPLETED
```

## 11. Prompt archive

Persist every meaningful prompt before execution.

Recommended structure:

```text
/prompts/YYYY/MM/DD/<prompt-id>.md
```

Each record contains:

```text
Prompt ID
Timestamp
Source
Session
Client
Project
Original prompt
Extracted requirements
Related missions
Related tasks
Related artifacts
Completion state
```

Never destroy the original prompt after summarization.

## 12. Promise tracking

Extract commitments from agent responses such as:

```text
"I will also add..."
"Next I will..."
"This includes..."
"The remaining step is..."
```

Convert them into tracked requirements when they constitute committed work. If no evidence is later found, classify `PROMISED_NOT_COMPLETED`.

## 13. False completion detector

Flag cases such as:

```text
Mission = DONE but unresolved requirement exists
Linear = DONE but expected artifact missing
Agent says complete but mandatory tests absent
PR merged but requested feature absent
Report delivered but required sections missing
```

Classification: `FALSELY_MARKED_DONE`.

## 14. Cross-session persistence

Mission state must survive restarts and session switching. Persist:

```text
Mission state
Original prompt references
Requirement graph
Task graph
Artifacts
Evidence
Agent ownership
Blockers
Next actions
Human approvals
```

Any compatible AGK agent must be able to resume from persisted state without asking what the mission was.

## 15. Session handoff object

Every cross-session or cross-agent handoff must contain:

```text
Mission
Goal
Original request
Requirements
Completed work
Remaining work
Current state
Relevant files
Relevant commits
Relevant tools
Evidence
Risks
Next action
```

A handoff is considered defective if the receiver must rediscover mission intent from scratch.

## 16. Global definition of done

A mission is DONE only when:

```text
All source prompts mapped
All requirements classified
No requested requirement silently omitted
All required tasks resolved
All acceptance criteria evaluated
All required artifacts exist
Mandatory verification passes
Required evidence exists
Completeness Oracle passes
Gauntlet passes
Requirement Graph has no unresolved executable nodes
Required human approvals are recorded
```

## 17. Core runtime law

```text
DO NOT TRUST AGENT MEMORY.
DO NOT TRUST "DONE".
DO NOT TRUST A SUMMARY AS THE ONLY SOURCE.

PERSIST THE ORIGINAL REQUEST.
EXTRACT THE REQUIREMENTS.
BUILD THE GRAPH.
VERIFY THE ARTIFACTS.
SEARCH FOR MISSING WORK.
LOOP UNTIL THE GRAPH IS CLOSED.
THEN PROVE COMPLETION.
```

Final principle:

```text
DO NOT MERELY ANSWER THE PROMPT.
CLOSE THE FULL REQUIREMENT GRAPH.
```
