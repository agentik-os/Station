# MEETING → TECH REPORT → BACKLOG → HUMAN GATE

AGK must ingest relevant meeting transcripts, summaries or notes and convert them into structured technical knowledge and candidate backlog items.

## Extract

```text
Decisions
Requests
Problems
Ideas
Risks
Open questions
Technical constraints
Product requirements
Dependencies
Potential bugs
Potential improvements
Potential missions
```

## Technical meeting report

Generate a structured recap separating:

```text
DECIDED
REQUESTED
SUGGESTED
DISCOVERED
ASSUMED
OPEN QUESTION
```

Never convert brainstorming into approved implementation automatically.

## Mapping

```text
MEETING
→ MEETING REPORT
→ TECHNICAL EXTRACTION
→ POTENTIAL MISSION
→ LINEAR BACKLOG CANDIDATE
→ WAIT FOR HUMAN
```

Backlog means documented opportunity, not authorized work.

## Human-gated execution

No coding agent may start work solely because a meeting, auditor, PM, QA or monitoring system created an issue.

Accepted activation sources may include:

```text
Discord explicit action
AGK UI explicit approval
Linear human state transition
CTO instruction
Authorized Product/Domain Leader instruction
Explicit client request under configured permissions
```

Record authorization provenance before moving to READY.

## Core law

```text
AGENTS MAY DISCOVER WORK.
AGENTS MAY DOCUMENT WORK.
AGENTS MAY PROPOSE WORK.
AGENTS MAY PREPARE WORK.

ONLY AN AUTHORIZED HUMAN MAY START NEW BACKLOG WORK.
```
