# Operative System project context template

## Ownership and trust boundaries

State the canonical Linux owner, Hermes profile, OS/package identity, owning agents, client/personal boundary, authorized A0–A3 actions, owner gates, and forbidden cross-boundary operations.

NEVER include API keys, tokens, passwords, secrets, credentials, private keys, or connection strings in reports, logs, commits, prompts, or evidence; replace any accidental occurrence with `[REDACTED]`.

## Memory, context, and Skills

Document what belongs in SOUL, USER, MEMORY, project context, Skills, package knowledge, sessions, and the CompletionStore. Keep procedures in Skills and project-specific rules here. Keep private/profile-local state in its canonical owner boundary.

## Repository workflow

List the canonical repository, branch/worktree convention, source and installed mirrors, test commands, build commands, generated-file rules, and expected artifacts. Require TDD for behavior changes and preserve unrelated dirty work.

## Verification and completion

Define every acceptance criterion, deterministic quality gate, artifact/evidence binding, coverage requirement, approval boundary, stop condition, and Completion Oracle requirement. Separate source readiness, deployed runtime state, delivery proof, and canonical completion.

## Fresh-session acceptance

Specify how a new Hermes session proves it can load this context, required Skills, bounded toolsets, durable inputs, and produce the artifact without relying on a previous chat. Bind the context and artifact hashes in the acceptance receipt.

## Deployment and rollback

Describe backup creation, atomic installation, owner/permission preservation, safe gateway drain/reload, exact live probes, rollback command, rollback evidence, and conditions that prevent cutover. Production cutover and irreversible deletion remain owner gates.
