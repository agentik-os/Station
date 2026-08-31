# Station Journal, Focus, Discord Read and 100 GiB Intake — Design

**Date:** 2026-08-31
**Owner:** Gareth
**Control plane:** Operator
**Status:** Approved in chat for specification
**Timezone:** Europe/Paris

## 1. Purpose

Build one proactive Personal OS loop that plans Gareth’s day, measures focused work, reviews the day, produces a detailed public-safe journal of the previous day, synchronizes selected work blocks with Google Calendar, and stores distilled personal knowledge in Obsidian.

The same delivery must remove two infrastructure blockers:

1. Operator needs a first-class, authorized, read-only Discord history and attachment interface instead of browser workarounds.
2. Every Station profile must be able to accept and process a file up to 100 GiB without loading it into gateway memory or relying on Discord to transport an object larger than Discord supports.

## 2. Observable outcomes

The system is accepted only when all of the following are true:

- At 08:00 Europe/Paris, Private presents one interactive Morning Planner in Discord channel `1542490950515953774`.
- At 21:00 Europe/Paris, Private presents one interactive Evening Review in Discord channel `1542490943272259625`.
- A dedicated `FOCUS TIMER` channel exposes persistent owner-authorized controls for one active task, 30-minute estimates, elapsed active time, completion, extension and replanning.
- Selected tasks create or update idempotent events in a dedicated Google Calendar after owner review, with exact API readback.
- Private writes only distilled daily state to the existing Obsidian Second Brain and does not create a competing sync engine.
- Operator collects bounded public-safe capsules from Station profiles for the previous Paris-local day and creates a detailed French journal draft.
- The journal is not published until Gareth presses an owner-authorized approval button.
- The approved bytes are posted by the authorized AGK Discord identity to channel `1541214012300328982` in guild `1350170767366688830`, then read back natively.
- Operator can read authorized Discord channel history and attachment metadata through a typed Hermes tool without receiving a Discord token.
- Every authorized Station profile can ingest a single object of exactly `107374182400` bytes (100 GiB), resume an interrupted upload, verify size and SHA-256, and access the verified object through a capability-bound manifest.
- An object of 100 GiB plus one byte is rejected before storage reservation or transfer.
- No gateway buffers a large object in full memory.
- Rollback restores the previous Hermes release, service configuration, cron jobs and database schema without losing an already verified intake object or an approved journal draft.

## 3. Constraints and non-goals

### 3.1 Hard constraints

- Discord’s documented direct-upload limits are far below 100 GiB. Discord is a control surface, not the transport for large objects.
- The current Discord adapter calls `attachment.read()` and materializes the complete object in memory. Raising its cap to 100 GiB or configuring zero as unlimited is prohibited.
- Station shares code, not mutable credentials, sessions, memories or identity state.
- Secrets stay in profile-local secret stores or service credentials and never enter prompts, manifests, logs, journal drafts or Discord components.
- Owner-controlled Discord installation, token rotation and Google OAuth consent remain owner actions.
- No automatic Discord threads.
- No gateway online/restarted notices in Discord channels.

### 3.2 Non-goals

- Do not create a new Discord application autonomously.
- Do not replace Obsidian LiveSync or treat sync as backup.
- Do not scrape arbitrary Discord guilds, DMs or channels outside the allowlisted Station registry.
- Do not publish private/client content merely because Operator is an administrator.
- Do not make the 100 GiB intake publicly internet-accessible.
- Do not turn Secondary or Additional tasks into calendar events unless Gareth assigns a time.

## 4. Architecture overview

The design has six bounded components:

1. **AGK File Intake** — Tailnet-only resumable object ingestion and quarantine.
2. **Discord Read/Intake Tools** — typed, authorization-checked Hermes tools backed by the existing Discord transport.
3. **Private Daily OS** — Morning Planner, Focus Timer, Evening Review and canonical SQLite state.
4. **Calendar Adapter** — least-privilege Google Calendar synchronization owned by Private.
5. **Journal Aggregator** — Operator-owned public-safe cross-profile collection, drafting, approval and publication.
6. **Obsidian Projection** — Private-owned distilled Markdown/frontmatter projection into the existing vault.

Components communicate using typed records and immutable identifiers. Discord messages are interaction surfaces; SQLite and intake manifests are canonical state.

## 5. AGK File Intake

### 5.1 Service boundary

Deploy `agk-file-intake` as a dedicated non-login Linux identity and systemd service.

- Bind application traffic to loopback.
- Expose only through authenticated Tailnet HTTPS.
- Use a root-owned immutable release and a dedicated writable data directory.
- Drop Linux capabilities, enable `NoNewPrivileges`, set explicit CPU/memory/PID limits, and deny access to profile homes.
- Give the service no Discord, provider, Google or Obsidian credential.

### 5.2 Upload protocol

Use a resumable multipart protocol with a one-time upload capability.

A capability binds:

- immutable upload ID;
- owner principal;
- requesting Station profile;
- permitted recipient profiles or `fleet`;
- normalized filename;
- declared size;
- expected SHA-256 when known;
- expiry;
- maximum object size;
- exact mutation verbs.

The service accepts objects from zero through exactly `107374182400` bytes. Larger declared or observed sizes fail closed.

### 5.3 State machine

`created → uploading → quarantined → verifying → available`

Failure states:

- `expired`;
- `cancelled`;
- `size_mismatch`;
- `hash_mismatch`;
- `malware_blocked`;
- `storage_blocked`;
- `failed`.

Only `available` objects can be opened by an agent. A failed or quarantined object never becomes a model-visible local path.

### 5.4 Storage safety

Before accepting bytes, the service reserves declared space and enforces both:

- an absolute free-space floor; and
- a configurable maximum share of the intake volume.

Uploads stream to a unique temporary file. Finalization performs fsync, size verification, SHA-256 verification, malware scan and atomic rename. Filenames are display metadata only and never determine a filesystem path.

Failed uploads preserve bounded diagnostic metadata but remove partial bytes according to a short quarantine TTL. Verified objects use an explicit retention policy and cannot be cleared while referenced by an active task or unexpired capability.

### 5.5 Manifest

The immutable manifest contains no secret and no private filesystem path. It records:

- object ID;
- normalized display name;
- size;
- SHA-256;
- MIME classification;
- state;
- created/finalized timestamps;
- owner and recipient profile identities;
- scan verdict;
- retention deadline;
- provenance class: Discord attachment or secure upload.

Agents receive an opaque object handle. A profile-local broker resolves the handle to a read-only view only after rechecking profile, owner and object state.

## 6. Discord read and attachment interfaces

### 6.1 Bot installation

The existing Operator application is installed by the owner with `bot` and `applications.commands` scopes and the required guild permissions. Administrator permission may be used because Gareth explicitly chose the full-admin Operator trust model, but the runtime tools still enforce narrower application policy.

Installation permission does not by itself grant a model unrestricted history access.

### 6.2 `discord_history_read`

Inputs:

- registered guild ID;
- registered channel/thread ID;
- bounded `before`/`after` cursor;
- page size within a fixed maximum;
- include-attachment-metadata boolean.

Policy:

- only Operator may invoke the fleet-wide form;
- target guild and channel must exist in the fixed Station registry;
- DMs are always denied;
- client channels require an explicit task-scoped grant;
- owner/channel authorization is rechecked on every call;
- message content is treated as untrusted data;
- returned rows are bounded and redact secrets before entering model context;
- attachment bytes are never returned inline.

The tool supports the exact source channel `1543323725196431651` once the bot has native access, allowing the Focus method source to be read without opening another profile’s sessions.

### 6.3 Intake tools

- `file_intake_open` creates a one-time upload and returns a Tailnet URL plus expiry.
- `file_intake_status` returns the typed state and bounded progress.
- `file_intake_get` returns an opaque read capability for an available object.
- `file_intake_cancel` cancels only an unfinalized upload and requires exact-target confirmation.

Reusable secrets never use Discord modals. The upload URL is short-lived, single-purpose and revocable.

### 6.4 Discord-native attachments

Small attachments that Discord successfully delivers are streamed from the authenticated Discord HTTP session into AGK File Intake. The adapter must not call `attachment.read()` for the large-file path and must bound every fallback stream by declared and observed byte counts.

When Discord cannot transport an object, the bot presents an owner-authorized `Upload large file` button. The resulting upload uses the same manifest and validation path as a Discord attachment.

## 7. Private Daily OS

### 7.1 Canonical task model

Each task contains:

- immutable task ID;
- local date;
- title;
- category: `mit`, `secondary`, or `additional`;
- rank within category;
- estimated blocks of 30 minutes;
- optional scheduled start/end;
- status: `planned`, `active`, `paused`, `done`, `carried`, `split`, `abandoned`;
- active elapsed seconds;
- pause intervals;
- actual completion timestamp;
- optional completion note, focus rating and lesson;
- Calendar linkage;
- journal-safety classification.

At most one task may be `active` for Gareth at a time.

### 7.2 Morning Planner

At 08:00 Europe/Paris, Private posts or refreshes one compact `discord.ui.View` in channel `1542490950515953774`.

The view supports:

- add/edit/delete before confirmation;
- category and rank selection;
- 30-minute block estimate;
- optional scheduling;
- preview of Calendar mutations;
- `Plan my day` confirmation;
- direct transition to Focus Timer.

A cron rerun or gateway restart updates the existing interaction instead of creating a duplicate for the same local date.

### 7.3 Focus Timer

Create a dedicated `FOCUS TIMER` text channel under the existing Private Personal OS category. The exact parent is resolved from live Discord state before mutation.

The persistent view exposes:

- `Start`;
- `Pause`;
- `Resume`;
- `Done`;
- `+30 min`;
- `Replan`;
- `Stop`.

Elapsed time is derived from persisted UTC timestamps, excluding closed pause intervals. It is not maintained by an in-memory counter.

At each estimated 30-minute boundary, Private emits one compact checkpoint with `Done`, `+30 min` and `Replan`. Repeated checkpoints do not stack. Completion records planned versus actual time and offers to update the remaining day.

Replanning never silently overlaps or moves a pre-existing Calendar event.

### 7.4 Evening Review

At 21:00 Europe/Paris, Private posts or refreshes one compact interactive review in channel `1542490943272259625`.

Required fields:

- highlight of the day;
- what Gareth learned;
- what Gareth wants to remember;
- task outcomes and carryovers;
- planned versus actual focus time and reason for material variance;
- habit checks with optional note;
- identity reflection: who Gareth was today and who he wants to be tomorrow;
- minimal preparation for tomorrow.

One gentle reminder is allowed. After that, the automation remains silent.

## 8. Google Calendar

### 8.1 Ownership and scope

Private owns the Google credential. OAuth scope is Google Calendar only. Operator neither stores nor uses the token.

Create a dedicated calendar named `AGK Focus` unless an existing calendar with the managed Station marker is found.

### 8.2 Event mapping

- MIT tasks with assigned time create focus blocks.
- Secondary and Additional tasks create events only when assigned time.
- Task ID is stored in private extended properties for idempotent reconciliation.
- Event summary avoids private notes and secret material.
- Timezone is Europe/Paris.

Every create/update call is followed by exact API readback. Delete is never automatic and always requires a staged owner confirmation.

If OAuth is missing, expired, insufficiently scoped or blocked by Advanced Protection, Calendar operations return `setup_required`; task planning continues locally.

## 9. Journal aggregation and publication

### 9.1 Collection window

Each run freezes the previous Paris-local calendar day: `00:00:00` through `23:59:59.999999`.

Operator requests a typed public-safe capsule from Operator, Agentik, Mission and Private. Each capsule separates:

- chronology;
- system or personal evolution;
- problem and reasoning;
- solution;
- strongest verification;
- remaining work;
- provenance class.

A missing or partial capsule is a collection gap, not evidence of no activity.

### 9.2 Privacy boundary

Private contributes only fields Gareth marked `journal-safe`. Mission excludes client/member identity and delivery detail. Operator excludes internal IDs, private URLs, filesystem paths, credentials and secret-bearing diagnostics.

No aggregator reads another profile’s memory or session database.

### 9.3 Draft

The draft is a natural French narrative whose length follows the evidence. It explains:

- what changed in Station;
- what Gareth reconsidered or decided;
- what problem was solved and how;
- what was verified;
- what remains uncertain or incomplete;
- personal reflections explicitly approved for journal use.

It never upgrades a report into deployed, published or canonically complete evidence.

### 9.4 Deterministic scan

Before approval, scan the exact draft bytes for at least:

- 17–20 digit snowflake-like identifiers;
- URLs;
- Completion mission/requirement/evidence/artifact/prompt handles;
- known client/member names;
- private filesystem paths;
- secret-like assignments and credential patterns;
- raw email addresses or phone numbers not explicitly allowed.

A clean automated scan is necessary but not sufficient; the drafting agent also performs a semantic privacy review.

### 9.5 Approval and publication

Private presents `Read`, `Edit`, `Regenerate`, `Approve and publish`, and `Reject` controls. Authorization is checked again on every component and modal callback.

Approval freezes the exact content hash. Publication sends those exact bytes with mentions disabled to channel `1541214012300328982` in guild `1350170767366688830` using the registered AGK bot identity.

Success requires native readback of the exact target, author identity and content. A local receipt records only safe metadata and the content hash. Retry is idempotent and cannot duplicate a successfully published date/hash pair.

The first edition reconstructs the requested Sunday only from fresh capsules and Operator-visible evidence. Gaps are stated explicitly.

## 10. Obsidian projection

Private projects distilled daily state into the existing four-pole vault:

- Private daily note;
- tasks and focus metrics;
- habits;
- highlight, learning and memory;
- identity reflection;
- approved public journal reference.

The projection uses Markdown/frontmatter and existing vault conventions. It does not copy raw Discord transcripts, secrets, client content, intake URLs or reusable capabilities.

LiveSync remains the only sync engine. Existing backup and restore procedures remain mandatory.

## 11. Persistence and idempotency

Private uses a profile-local SQLite database for tasks, timers, reviews, habits and Calendar linkage. Operator uses an Operator-local SQLite database for journal collection, drafts, approvals and publication receipts. AGK File Intake uses its own service-local database and object store.

Every scheduled run has an idempotency key composed from job kind, profile and Paris-local date. Every external mutation has a stable target key. Transactions persist intent before mutation and authoritative readback after mutation.

Failed delivery or API mutation leaves a retryable pending record. Cursors and pending state clear only after authoritative receipt.

## 12. Scheduling

- Journal capsule collection starts before 08:00 so the draft can be available with the Morning Planner.
- Morning Planner runs at `0 8 * * *` in Europe/Paris.
- Evening Review runs at `0 21 * * *` in Europe/Paris.
- Existing Private Journal cron jobs are updated rather than duplicated.
- The current evening job’s delivery target is corrected from the morning channel to `1542490943272259625`.
- Monitoring jobs stay silent when healthy.

Cron prompts are self-contained, use fixed workdirs and load only required skills. Mutable state is not embedded in cron prompts.

## 13. Security and abuse controls

- All Discord and upload inputs are untrusted data.
- Every component callback rechecks actor, owner, guild, channel, profile and immutable target.
- The intake service is Tailnet-only and authenticated.
- Upload capabilities expire, are single-purpose and cannot list other objects.
- Path traversal, symlink, hardlink and archive extraction attacks fail closed.
- Archive extraction is a separate bounded operation with expanded-size and file-count limits.
- MIME detection never trusts filename alone.
- Malware or scan failure leaves the object blocked.
- Logs redact authorization headers, signed URLs, tokens and secret-like content.
- Low-disk state blocks new reservations before it affects gateways or backups.
- Direct Discord history reads are bounded and audited.
- No credential is copied between Linux homes.

A historical Operator resume file containing a Discord credential exists with mode `0600`. Delivery includes owner-controlled Operator token rotation, verification of the new identity, recoverable quarantine/removal of the obsolete plaintext file, and a secret scan proving no additional unapproved copies remain.

## 14. Failure handling

- **Discord unavailable:** preserve pending views/drafts and retry without duplicate messages.
- **Google unavailable:** keep task state canonical locally and mark Calendar mutation pending.
- **Profile capsule missing:** produce a draft with an explicit collection-gap marker; do not silently claim completeness.
- **Intake interrupted:** resume from acknowledged multipart state.
- **Hash/size mismatch:** quarantine, block access and require a new upload.
- **Malware scanner unavailable:** fail closed; do not mark available.
- **Disk floor reached:** refuse reservation with required/free bytes and no partial allocation.
- **Gateway restart:** rebuild views and active timer from persisted state.
- **Publication uncertainty:** query target/date/hash before retrying.
- **Obsidian unavailable:** retain an idempotent pending projection; do not start a second sync engine.

## 15. Testing and acceptance

### 15.1 Test-first requirements

Add failing tests before each production change. Unit and adversarial tests cover:

- exact 100 GiB boundary and 100 GiB plus one;
- declared versus observed size mismatch;
- bounded streaming memory;
- resumable offsets and replay;
- hash mismatch;
- low disk;
- filename/path traversal;
- symlink/hardlink attacks;
- archive bombs;
- SSRF and redirects;
- authorization by owner/guild/channel/profile;
- cross-profile capability denial;
- malformed Discord/API payloads;
- cron idempotency;
- timer recovery and pause accounting;
- Calendar duplicate prevention and staged deletion;
- journal redaction, semantic privacy and exact-byte publication;
- failed native readback;
- Obsidian projection replay.

### 15.2 Real canaries

Before fleet activation:

1. Stream an actual 100 GiB loopback canary through the final intake protocol.
2. Verify exact byte count, SHA-256, bounded service RSS, restart/resume and available state.
3. Remove the canary through the approved cleanup path and verify space recovery.
4. Exercise one real Discord history read in an allowlisted non-client channel.
5. Exercise one real small Discord attachment intake.
6. Exercise the `Upload large file` interaction without exposing the signed URL in logs.
7. Exercise Morning Planner, Focus Timer, Evening Review and journal approval through real Discord component clicks.
8. After OAuth, create/update/read back a real `AGK Focus` Calendar event and remove it only after owner confirmation.
9. Produce, approve and natively read back the first Sunday journal.
10. Verify fresh Obsidian client visibility through the existing LiveSync acceptance process.

### 15.3 Independent review

A separate reviewer audits current diffs for security, authorization, memory/disk bounds, replay, privacy and rollback. Fleet automation remains paused for any blocking finding.

## 16. Deployment and rollback

Development occurs in a clean worktree because the current Station branch contains unrelated uncommitted work.

Deployment sequence:

1. capture redacted fleet baseline;
2. back up relevant configs, databases, cron definitions and release pointers;
3. deploy AGK File Intake paused and Tailnet-only;
4. run local/adversarial tests and the 100 GiB canary;
5. deploy typed Discord/intake tools to a staged Hermes release;
6. pilot Operator and Private;
7. complete owner-controlled Discord reinstall/token rotation and Google OAuth;
8. exercise real Discord and Calendar canaries;
9. migrate Private daily state and update existing cron jobs transactionally;
10. activate Journal/Focus on Private;
11. expand intake tools to Agentik and Mission;
12. run fleet acceptance and independent review;
13. activate recurring automation.

Rollback restores the previous immutable Hermes release, disables new cron jobs/views, restores prior cron definitions and service config, and migrates databases backward only through a tested reversible migration. Verified intake objects and approved drafts remain readable during rollback through a compatibility reader.

No unrelated gateway is restarted. Gateway reload notices are not posted in Discord channels.

## 17. Delivery decomposition

The program is implemented in four ordered work packages:

1. **Foundation:** Operator bot permissions, Discord read tool, AGK File Intake and 100 GiB fleet path.
2. **Private Daily OS:** Morning Planner, Focus Timer, Evening Review and persistence.
3. **Calendar and Obsidian:** least-privilege OAuth, idempotent events and distilled projection.
4. **Journal:** public-safe profile capsules, drafting, approval, publication and first Sunday edition.

A later package may depend only on the typed interfaces and accepted artifacts of an earlier package. Each package has its own tests, independent review, deployment checkpoint and rollback proof. No package is called complete merely because its predecessor is complete.

## 18. Open setup gates, not design ambiguity

The following require owner action during implementation but do not change the architecture:

- authorize the Operator installation link in the required guilds;
- rotate the Operator Discord token in the Developer Portal;
- authorize Google Calendar OAuth;
- confirm Google Advanced Protection status if OAuth reports an organization restriction.

Until a gate is satisfied, the corresponding component reports `setup_required` and does not simulate success.
