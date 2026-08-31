# Station Public-Safe Daily Journal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a detailed French journal of the previous day from public-safe Station evidence, require Gareth’s Discord approval, publish exact approved bytes, and verify native readback.

**Architecture:** Profiles create typed public-safe capsules through the existing interagent boundary. Operator persists a frozen collection window, drafts and scans the journal; Private hosts the approval View; the authorized AGK bot publishes idempotently to the exact external channel.

**Tech Stack:** Python 3.11, SQLite WAL, Station interagent broker, discord.py persistent Views, Hermes cron, pytest.

**Spec:** `docs/superpowers/specs/2026-08-31-station-journal-focus-large-file-design.md`

## Global Constraints

- Collection window is the previous Europe/Paris local calendar day.
- Sources are Operator, Agentik, Mission and Private public-safe capsules; missing sources are explicit gaps.
- Never read another profile’s memory/session database to fill a gap.
- Reject snowflakes, URLs, Completion handles, client/member names, private paths, contact details and secret-like assignments from publishable bytes.
- Private contributes only fields explicitly marked `journal_safe`.
- Publication requires Gareth’s `Approve and publish` click.
- Publish to guild `1350170767366688830`, channel `1541214012300328982`, with mentions disabled.
- Success requires exact native author/target/content readback and idempotent date/hash receipt.

## File map

- `overlay/scripts/station_journal/domain.py` — windows, capsules, draft and receipts.
- `overlay/scripts/station_journal/store.py` — Operator-local SQLite, pending delivery and idempotency.
- `overlay/scripts/station_journal/collect.py` — bounded interagent capsule requests.
- `overlay/scripts/station_journal/sanitize.py` — deterministic byte scanner.
- `overlay/scripts/station_journal/draft.py` — closed-evidence French journal prompt/renderer boundary.
- `overlay/scripts/station_journal/publish.py` — exact-byte Discord publication/readback.
- `overlay/hermes/plugins/agentik_os/journal_capsule.py` — per-profile capsule tool/handler.
- `overlay/hermes/plugins/platforms/discord/agk_journal_ui.py` — Gareth approval View.
- `overlay/scripts/station_journal_cron.py` — collect/draft/recover runner.
- `overlay/scripts/install-station-journal.py` — cron/View installation and rollback.
- `tests/test_station_journal_domain.py`, `tests/test_station_journal_collection.py`, `tests/test_station_journal_sanitize.py`, `tests/test_station_journal_ui.py`, `tests/test_station_journal_publish.py`, `tests/test_station_journal_installer.py`.

---

### Task 1: Frozen Paris-local window and capsule schema

**Files:**
- Create: `overlay/scripts/station_journal/__init__.py`
- Create: `overlay/scripts/station_journal/domain.py`
- Test: `tests/test_station_journal_domain.py`

**Interfaces:**
- Produces: `JournalWindow.previous_day(now, ZoneInfo("Europe/Paris"))`, `JournalCapsule`, `EvidenceItem`, `CollectionStatus`, `PublicationReceipt`.

- [ ] **Step 1: Write failing DST/window/schema tests**

```python
window = JournalWindow.previous_day(datetime(2026,8,31,8,tzinfo=PARIS))
assert window.local_date.isoformat() == "2026-08-30"
assert window.local_start.hour == 0
assert window.local_end.date() == window.local_date
```

Include 23-hour and 25-hour DST days; reject capsule source outside `operator|agentik|mission|private` and evidence without provenance.

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_station_journal_domain.py`
Expected: FAIL because module is missing.

- [ ] **Step 3: Implement immutable types and exact JSON serialization**

Capsule fields: source, window, chronology, evolution, problems, solutions, decisions, learning, verification, remaining, gaps, provenance. Bound each list and string.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_station_journal_domain.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add overlay/scripts/station_journal tests/test_station_journal_domain.py
git commit -m "feat(journal): define public-safe daily evidence"
```

### Task 2: Operator-local transactional journal store

**Files:**
- Create: `overlay/scripts/station_journal/store.py`
- Test: `tests/test_station_journal_domain.py`

**Interfaces:**
- Produces: `JournalStore.open(operator_home)`, `create_run(window)`, `record_capsule`, `record_gap`, `save_draft`, `freeze_approval`, `mark_pending_publish`, `record_publication`.

- [ ] **Step 1: Write failing concurrency and idempotency tests**

Two collectors claim one date; capsules upsert by source/window; approval freezes SHA-256; changed bytes invalidate approval; publication date/hash pair is unique.

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_station_journal_domain.py -k store`
Expected: FAIL.

- [ ] **Step 3: Implement SQLite WAL schema**

Tables: `runs`, `capsules`, `gaps`, `drafts`, `approvals`, `pending_publications`, `publication_receipts`, `view_receipts`. Use `BEGIN IMMEDIATE`, UTC timestamps and mode-0600 DB.

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_station_journal_domain.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add overlay/scripts/station_journal/store.py tests/test_station_journal_domain.py
git commit -m "feat(journal): persist drafts and publication receipts"
```

### Task 3: Per-profile public-safe capsule handler

**Files:**
- Create: `overlay/hermes/plugins/agentik_os/journal_capsule.py`
- Modify: `overlay/hermes/plugins/agentik_os/__init__.py`
- Test: `tests/test_station_journal_collection.py`

**Interfaces:**
- Produces Hermes tool `journal_capsule` actions `prepare|submit|status` and profile-specific providers returning `JournalCapsule`.

- [ ] **Step 1: Write failing profile/privacy tests**

Operator provider can use current operational evidence; Private reads only `journal_safe` Daily OS rows; Mission rejects client/member fields; Agentik excludes credentials/private URLs. Wrong window/profile is denied.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_station_journal_collection.py -k capsule`
Expected: FAIL.

- [ ] **Step 3: Implement typed providers and schema**

Provider input is exact window and request ID. Output contains structured facts, not prose transcripts. Enforce source-local allowlists and deterministic redaction before broker submission.

- [ ] **Step 4: Register the tool without exposing cross-profile filesystem access**

```python
ctx.register_tool(name="journal_capsule", toolset="file", schema=JOURNAL_CAPSULE_SCHEMA, handler=handle_journal_capsule)
```

- [ ] **Step 5: Run collection and plan-gate tests**

Run: `pytest -q tests/test_station_journal_collection.py tests/test_completion_plugin.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/hermes/plugins/agentik_os/journal_capsule.py overlay/hermes/plugins/agentik_os/__init__.py tests/test_station_journal_collection.py tests/test_completion_plugin.py
git commit -m "feat(journal): collect profile-safe daily capsules"
```

### Task 4: Bounded interagent collection

**Files:**
- Create: `overlay/scripts/station_journal/collect.py`
- Modify: `overlay/scripts/station_interagent_broker.py`
- Test: `tests/test_station_journal_collection.py`

**Interfaces:**
- Produces: `JournalCollector.collect(window, sources, deadline) -> CollectionResult`.

- [ ] **Step 1: Write failing silence/malformed/ordering tests**

Assert same request to four sources, exact request IDs, source authentication, late response ignored for current run, malformed response becomes gap, and peer silence never becomes an empty successful capsule.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_station_journal_collection.py -k collector`
Expected: FAIL.

- [ ] **Step 3: Implement collection over existing UID-authenticated broker**

Persist request before send, accept one source-authenticated response per source/window, bound deadline and payload, and expose gaps separately.

- [ ] **Step 4: Run broker regressions**

Run: `pytest -q tests/test_station_journal_collection.py tests/test_interagent_smart_policy.py tests/test_interagent_soft_threads.py`
Expected: PASS with no Discord thread creation for ordinary capsule messages.

- [ ] **Step 5: Commit**

```bash
git add overlay/scripts/station_journal/collect.py overlay/scripts/station_interagent_broker.py tests/test_station_journal_collection.py
git commit -m "feat(journal): collect bounded Station evidence"
```

### Task 5: Deterministic publish-byte scanner

**Files:**
- Create: `overlay/scripts/station_journal/sanitize.py`
- Test: `tests/test_station_journal_sanitize.py`

**Interfaces:**
- Produces: `scan_publishable(text: str, policy: SafetyPolicy) -> ScanReport`; `require_publishable`.

- [ ] **Step 1: Write adversarial failing tests**

Cover 17–20 digit IDs, URLs, nested/encoded URLs, Completion handles, known names, `/home/...` paths, token/password/api-key assignments, emails, phones, zero-width characters, Markdown links and benign dates/counts.

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_station_journal_sanitize.py`
Expected: FAIL.

- [ ] **Step 3: Implement staged normalization and full-token capture**

Normalize Unicode, inspect original and normalized bytes, decode bounded URL encodings, scan recursive Markdown link targets, and return finding class/offset only—never echo the sensitive token.

- [ ] **Step 4: Add exact report fields**

`byte_count`, `line_count`, counts by finding class, and clean boolean. `require_publishable` raises a safe error with counts only.

- [ ] **Step 5: Run tests**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_station_journal_sanitize.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/scripts/station_journal/sanitize.py tests/test_station_journal_sanitize.py
git commit -m "feat(journal): reject unsafe publication bytes"
```

### Task 6: Closed-evidence French drafting

**Files:**
- Create: `overlay/scripts/station_journal/draft.py`
- Test: `tests/test_station_journal_domain.py`
- Test: `tests/test_station_journal_sanitize.py`

**Interfaces:**
- Produces: `DraftInput.from_collection(result)`, `build_closed_evidence_prompt`, `validate_draft_claims`, `DraftResult`.

- [ ] **Step 1: Write failing claim/provenance tests**

A draft may use only capsule facts; missing sources must appear as gaps; historical self-report cannot become live deployment; no fabricated test counts; personal content requires `journal_safe` provenance.

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_station_journal_domain.py -k draft`
Expected: FAIL.

- [ ] **Step 3: Implement closed evidence packet and deterministic validation**

Prompt includes exact date, structured capsules, explicit forbidden classes and requested French narrative. Store packet hash and model output hash. Validate cited evidence item IDs internally, then remove internal references before safety scan.

- [ ] **Step 4: Pass final bytes through scanner and semantic privacy gate**

Draft state remains `blocked` until deterministic scan clean and semantic reviewer returns explicit PASS.

- [ ] **Step 5: Run tests**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_station_journal_domain.py tests/test_station_journal_sanitize.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/scripts/station_journal/draft.py tests/test_station_journal_domain.py tests/test_station_journal_sanitize.py
git commit -m "feat(journal): draft from closed public-safe evidence"
```

### Task 7: Gareth approval View

**Files:**
- Create: `overlay/hermes/plugins/platforms/discord/agk_journal_ui.py`
- Modify: `overlay/hermes/plugins/platforms/discord/adapter.py`
- Test: `tests/test_station_journal_ui.py`

**Interfaces:**
- Produces: `JournalApprovalView`, `ensure_journal_draft_view(adapter, run_id)` with `Read`, `Edit`, `Regenerate`, `Approve and publish`, `Reject`.

- [ ] **Step 1: Write failing authorization/freeze tests**

Wrong actor/channel/run denied; approval allowed only for scan-clean current draft; edit/regenerate invalidates prior approval; component restart rehydrates same message; content preview handles Discord length without losing full draft.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_station_journal_ui.py`
Expected: FAIL.

- [ ] **Step 3: Implement persistent compact View**

`Read` attaches or paginates safe draft; `Edit` uses bounded modal sections; `Approve` stores exact SHA-256 then queues publication; no callback publishes before transaction commit.

- [ ] **Step 4: Run UI policy tests**

Run: `pytest -q tests/test_station_journal_ui.py tests/test_agk_discord_ui_policy.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add overlay/hermes/plugins/platforms/discord/agk_journal_ui.py overlay/hermes/plugins/platforms/discord/adapter.py tests/test_station_journal_ui.py
git commit -m "feat(journal): add owner approval surface"
```

### Task 8: Exact-byte publication and native readback

**Files:**
- Create: `overlay/scripts/station_journal/publish.py`
- Test: `tests/test_station_journal_publish.py`

**Interfaces:**
- Produces: `JournalPublisher.publish(run_id) -> PublicationReceipt`, fixed target guild/channel and registered bot identity.

- [ ] **Step 1: Write failing idempotency/readback tests**

Assert no approval blocks publish; changed hash blocks; mentions disabled; uncertain POST triggers search/readback before retry; exact date/hash pair never duplicates; wrong author/target/content is failure.

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_station_journal_publish.py`
Expected: FAIL.

- [ ] **Step 3: Implement explicit Discord payload and readback**

Set exact target constants, `allowed_mentions={"parse":[]}`, split only at deterministic paragraph boundaries while preserving approved aggregate bytes, and persist message IDs only after GET readback validates author/channel/content.

- [ ] **Step 4: Implement pending replay**

On timeout, query stored/target messages by bounded after-cursor and approved content hash before another POST. Keep pending until authoritative receipt.

- [ ] **Step 5: Run tests**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_station_journal_publish.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/scripts/station_journal/publish.py tests/test_station_journal_publish.py
git commit -m "feat(journal): publish approved bytes idempotently"
```

### Task 9: Cron runner and transactional installer

**Files:**
- Create: `overlay/scripts/station_journal_cron.py`
- Create: `overlay/scripts/install-station-journal.py`
- Modify: `overlay/install.sh`
- Test: `tests/test_station_journal_installer.py`

**Interfaces:**
- Runner: `station_journal_cron.py collect|draft|recover --date YYYY-MM-DD`.
- Installer: `install-station-journal.py check|install|rollback`.

- [ ] **Step 1: Write failing schedule/recovery tests**

Assert collection before 08:00 Europe/Paris, draft attached to morning surface, existing jobs updated not duplicated, missing source preserved, pending publication replayed, and healthy runs silent.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_station_journal_installer.py`
Expected: FAIL.

- [ ] **Step 3: Implement self-contained cron runner**

Runner fixes exact date/window, persists state, invokes one stage, prints user content only for actionable draft/error and uses safe structured local receipts.

- [ ] **Step 4: Implement installer backup/update/readback**

Install Operator collection/draft cron, attach approval to Private morning surface, preserve existing Journal cron IDs, back up definitions and support exact rollback.

- [ ] **Step 5: Run affected tests**

Run: `pytest -q tests/test_station_journal_*.py tests/test_station_contract.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/scripts/station_journal_cron.py overlay/scripts/install-station-journal.py overlay/install.sh tests/test_station_journal_installer.py
git commit -m "feat(journal): schedule recoverable daily publication"
```

### Task 10: First Sunday journal and live acceptance

**Files:**
- Create: `docs/runbooks/station-daily-journal.md`
- Create: `docs/release-manifests/station-daily-journal-v1.json`

**Interfaces:**
- Produces redacted collection, scan, approval, publication and native-readback receipt.

- [ ] **Step 1: Deploy with recurring publication paused**

Run full affected suite, install staged shared release and read back cron/View state without publishing.

- [ ] **Step 2: Collect fresh Sunday capsules**

Use exact Sunday window. Require Operator, Agentik, Mission and Private capsule status. Preserve every missing/partial source as a gap.

- [ ] **Step 3: Generate and scan the real draft**

Record byte/line counts and zero findings; perform semantic privacy review; expose draft in the real approval View.

- [ ] **Step 4: Gareth clicks the real approval button**

Verify frozen content hash and pending publication row before Discord mutation.

- [ ] **Step 5: Publish and read back natively**

Verify bot identity, exact target, aggregate content bytes and message IDs. Retry test must not duplicate.

- [ ] **Step 6: Exercise restart and rollback**

Recover same draft/View after controlled reload; rollback jobs/code and reapply without losing receipt.

- [ ] **Step 7: Independent privacy/security review and activation**

Resolve blocking findings, then enable recurring jobs and read back next schedules.

- [ ] **Step 8: Commit safe release evidence**

```bash
git add docs/runbooks/station-daily-journal.md docs/release-manifests/station-daily-journal-v1.json
git commit -m "docs(journal): record first native daily publication"
```
