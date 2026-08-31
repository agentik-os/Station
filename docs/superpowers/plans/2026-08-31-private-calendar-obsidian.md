# Private Calendar and Obsidian Projection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Synchronize explicitly scheduled Personal OS tasks to a dedicated Google Calendar and project distilled daily state into the existing Obsidian vault.

**Architecture:** Private owns a narrow Calendar adapter and its OAuth token. Calendar reconciliation is idempotent through Station task IDs; Obsidian projection writes deterministic Markdown/frontmatter through the existing vault and LiveSync boundary.

**Tech Stack:** Python 3.11, Google Calendar REST/OAuth, SQLite linkage from Daily OS, Markdown/frontmatter, existing Obsidian LiveSync, pytest.

**Spec:** `docs/superpowers/specs/2026-08-31-station-journal-focus-large-file-design.md`

## Global Constraints

- OAuth scope is Calendar only and credentials remain under Private.
- Calendar name is `AGK Focus`; existing managed calendar is reused by its private marker.
- MIT tasks sync only when scheduled; Secondary/Additional sync only when Gareth assigns a time.
- Create/update requires preview and exact API readback; delete always requires staged owner confirmation.
- OAuth failure yields `setup_required`; local planning continues.
- Obsidian receives distilled information only, never raw Discord transcripts, credentials, intake URLs or client content.
- Do not introduce a second vault sync engine.

## File map

- `overlay/hermes/plugins/agentik_os/personal_daily/calendar.py` — Calendar domain and reconciliation.
- `overlay/hermes/plugins/agentik_os/personal_daily/google_calendar_client.py` — narrow REST client/token refresh boundary.
- `overlay/hermes/plugins/agentik_os/personal_daily/obsidian_projection.py` — deterministic daily note projection.
- `overlay/hermes/plugins/platforms/discord/agk_personal_daily_ui.py` — preview/confirm Calendar controls.
- `overlay/scripts/install-personal-calendar.py` — setup check and managed calendar bootstrap.
- `tests/test_personal_calendar.py`, `tests/test_personal_calendar_ui.py`, `tests/test_personal_obsidian_projection.py`, `tests/test_personal_calendar_installer.py`.

---

### Task 1: Calendar mutation model and eligibility

**Files:**
- Create: `overlay/hermes/plugins/agentik_os/personal_daily/calendar.py`
- Test: `tests/test_personal_calendar.py`

**Interfaces:**
- Produces: `CalendarIntent`, `CalendarReceipt`, `eligible_for_calendar(task)`, `build_event(task, timezone="Europe/Paris")`.

- [ ] **Step 1: Write failing eligibility tests**

```python
assert eligible_for_calendar(scheduled_mit) is True
assert eligible_for_calendar(unscheduled_mit) is False
assert eligible_for_calendar(scheduled_secondary) is True
assert eligible_for_calendar(unscheduled_additional) is False
```

Assert private extended property `agk_station_task_id` and no private note in summary/description.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_calendar.py -k eligibility`
Expected: FAIL because module is missing.

- [ ] **Step 3: Implement frozen intent/receipt types**

Event start/end must include Europe/Paris offset, end must be after start, and summary is bounded to task title without completion notes.

- [ ] **Step 4: Run tests**

Run: `pytest -q tests/test_personal_calendar.py -k eligibility`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add overlay/hermes/plugins/agentik_os/personal_daily/calendar.py tests/test_personal_calendar.py
git commit -m "feat(private): model focus calendar intents"
```

### Task 2: Narrow Google Calendar client

**Files:**
- Create: `overlay/hermes/plugins/agentik_os/personal_daily/google_calendar_client.py`
- Test: `tests/test_personal_calendar.py`

**Interfaces:**
- Produces: `GoogleCalendarClient.list_managed_calendar`, `create_managed_calendar`, `find_by_task_id`, `create_event`, `update_event`, `get_event`, `delete_event`.

- [ ] **Step 1: Write failing HTTP contract tests**

Use a fake transport. Assert Calendar-only scope, bearer header redacted from exceptions, bounded response bodies, 401 refresh once, 403 maps to `SetupRequired`, 429/5xx retryable, and malformed JSON fails closed.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_calendar.py -k client`
Expected: FAIL.

- [ ] **Step 3: Implement client with injected transport and token provider**

Use exact Calendar v3 endpoints, RFC3339 timestamps, `privateExtendedProperty=agk_station_task_id=<id>`, explicit request timeout and typed safe errors. Do not log headers or response bodies.

- [ ] **Step 4: Implement managed calendar marker**

Store `{"agk_station_managed":"focus-v1"}` in calendar description and reuse only an exact marker match; name match alone is insufficient.

- [ ] **Step 5: Run tests**

Run: `pytest -q tests/test_personal_calendar.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/hermes/plugins/agentik_os/personal_daily/google_calendar_client.py tests/test_personal_calendar.py
git commit -m "feat(private): add least-privilege calendar client"
```

### Task 3: Idempotent Calendar reconciliation

**Files:**
- Modify: `overlay/hermes/plugins/agentik_os/personal_daily/calendar.py`
- Modify: `overlay/hermes/plugins/agentik_os/personal_daily/store.py`
- Test: `tests/test_personal_calendar.py`

**Interfaces:**
- Produces: `CalendarReconciler.preview(task) -> CalendarIntent`, `apply(intent) -> CalendarReceipt`, `stage_delete(task_id) -> DeleteIntent`, `confirm_delete(intent_id) -> CalendarReceipt`.

- [ ] **Step 1: Write failing duplicate/readback tests**

Apply the same intent twice; assert one remote event. Update scheduled time; assert same event ID and exact readback. Simulate create timeout followed by retry; query by task ID before creating again.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_calendar.py -k reconcile`
Expected: FAIL.

- [ ] **Step 3: Implement persisted pending intent before API mutation**

Transaction order: save intent → query exact task property → create/update → get event → compare expected fields → persist receipt. Leave pending on uncertain failure.

- [ ] **Step 4: Implement two-stage deletion**

Delete intent expires after ten minutes, binds owner/task/event ID and cannot execute twice. After delete, `get_event` must return not found before receipt is final.

- [ ] **Step 5: Run tests**

Run: `pytest -q tests/test_personal_calendar.py tests/test_personal_daily_store.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/hermes/plugins/agentik_os/personal_daily/calendar.py overlay/hermes/plugins/agentik_os/personal_daily/store.py tests/test_personal_calendar.py tests/test_personal_daily_store.py
git commit -m "feat(private): reconcile focus events idempotently"
```

### Task 4: Calendar preview and confirmation UI

**Files:**
- Modify: `overlay/hermes/plugins/platforms/discord/agk_personal_daily_ui.py`
- Test: `tests/test_personal_calendar_ui.py`

**Interfaces:**
- Produces: `CalendarPreviewView` with `Apply`, `Back`, `Close`; `CalendarDeleteConfirmView` with staged confirm.

- [ ] **Step 1: Write failing owner and exact-target tests**

Wrong actor, expired intent, mismatched task/event or wrong channel is denied. `Apply` shows proposed summary/start/end/calendar before mutation. Success displays read-back time and event state, not credentials or raw API body.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_calendar_ui.py`
Expected: FAIL.

- [ ] **Step 3: Implement compact Views**

Use buttons for `Apply`, `Back`, `Close`; deletion uses `Delete event` then an ephemeral `Confirm delete` interaction. Recheck state in callback rather than trusting View fields.

- [ ] **Step 4: Run UI tests**

Run: `pytest -q tests/test_personal_calendar_ui.py tests/test_agk_discord_ui_policy.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add overlay/hermes/plugins/platforms/discord/agk_personal_daily_ui.py tests/test_personal_calendar_ui.py
git commit -m "feat(private): confirm focus calendar mutations"
```

### Task 5: OAuth/setup gate and managed calendar installer

**Files:**
- Create: `overlay/scripts/install-personal-calendar.py`
- Modify: `overlay/install.sh`
- Test: `tests/test_personal_calendar_installer.py`
- Create: `docs/runbooks/personal-calendar.md`

**Interfaces:**
- Produces CLI: `install-personal-calendar.py check|bootstrap|rollback` returning `authenticated|setup_required|ready` without token details.

- [ ] **Step 1: Write failing setup tests**

Assert missing token, insufficient scope, expired refresh, Advanced Protection 403 and disabled Calendar API all return safe `setup_required`; successful bootstrap reuses exact managed marker.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_calendar_installer.py`
Expected: FAIL.

- [ ] **Step 3: Implement Private-only setup discovery**

Resolve credentials from Private’s Google Workspace OAuth store, verify Calendar scope without printing values, and refuse Operator/Agentik/Mission homes.

- [ ] **Step 4: Document owner OAuth gate**

Use Calendar-only service selection. If organization restriction appears, instruct Gareth to allowlist the OAuth client; do not broaden scopes.

- [ ] **Step 5: Wire script installation and run tests**

Run: `pytest -q tests/test_personal_calendar_installer.py tests/test_station_contract.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/scripts/install-personal-calendar.py overlay/install.sh tests/test_personal_calendar_installer.py docs/runbooks/personal-calendar.md
git commit -m "feat(private): gate calendar OAuth and bootstrap"
```

### Task 6: Deterministic Obsidian daily projection

**Files:**
- Create: `overlay/hermes/plugins/agentik_os/personal_daily/obsidian_projection.py`
- Test: `tests/test_personal_obsidian_projection.py`

**Interfaces:**
- Produces: `DailyProjection.from_store(day)`, `render_daily_markdown(projection)`, `project_daily_note(vault_root, projection) -> ProjectionReceipt`.

- [ ] **Step 1: Write failing content/privacy tests**

Assert tasks, estimated/actual time, habits, highlight, learning, memory and identity reflection appear; raw Discord content, credentials, URLs, client names, intake capabilities and private paths do not. Repeated projection produces identical bytes.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_obsidian_projection.py`
Expected: FAIL.

- [ ] **Step 3: Implement fixed frontmatter and section order**

Use clean folder names without numeric prefixes, `pole/private` and transversal tracking tags, normalized LF, YAML-safe scalars and atomic mode-preserving replace.

- [ ] **Step 4: Add symlink/path traversal refusal**

Open vault and parent directories with no-follow checks; write only under the configured Private daily-note root.

- [ ] **Step 5: Run tests**

Run: `pytest -q tests/test_personal_obsidian_projection.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/hermes/plugins/agentik_os/personal_daily/obsidian_projection.py tests/test_personal_obsidian_projection.py
git commit -m "feat(private): project daily state to Obsidian"
```

### Task 7: Pending projection replay and LiveSync acceptance

**Files:**
- Modify: `overlay/hermes/plugins/agentik_os/personal_daily/store.py`
- Modify: `overlay/scripts/personal_daily_cron.py`
- Test: `tests/test_personal_obsidian_projection.py`
- Modify: `docs/runbooks/personal-calendar.md`

**Interfaces:**
- Produces: pending projection queue and authoritative hash receipt.

- [ ] **Step 1: Write failing replay tests**

Simulate unavailable vault and interrupted atomic replace; assert pending row remains. Replay twice; assert one final note and receipt hash. Never start/stop LiveSync from projection code.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_obsidian_projection.py -k replay`
Expected: FAIL.

- [ ] **Step 3: Implement transactional pending projection queue**

Persist normalized payload hash before write; clear only after file hash readback. Cron healthy/no-op output remains empty.

- [ ] **Step 4: Run tests**

Run: `pytest -q tests/test_personal_obsidian_projection.py tests/test_personal_daily_cron.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add overlay/hermes/plugins/agentik_os/personal_daily/store.py overlay/scripts/personal_daily_cron.py tests/test_personal_obsidian_projection.py docs/runbooks/personal-calendar.md
git commit -m "feat(private): replay durable Obsidian projections"
```

### Task 8: Real Calendar and fresh-client Obsidian acceptance

**Files:**
- Create: `docs/release-manifests/personal-calendar-obsidian-v1.json`
- Modify: `docs/runbooks/personal-calendar.md`

**Interfaces:**
- Produces redacted receipts for OAuth scope, event create/update/readback/delete, note hash and fresh-client sync.

- [ ] **Step 1: Complete owner Calendar-only OAuth**

Run setup check and verify scope names without token values. If blocked, record `setup_required` and stop Calendar live acceptance.

- [ ] **Step 2: Create and read back one real focus event**

From a real MIT task, preview, click Apply, verify calendar marker, task property, summary/start/end and event ID.

- [ ] **Step 3: Update then owner-confirm deletion**

Verify same event ID after update; stage deletion; click confirm; read back not-found. No automatic deletion.

- [ ] **Step 4: Project one real daily note**

Read back exact file hash, run existing LiveSync sync, fetch in a fresh isolated client and compare relative path/hash.

- [ ] **Step 5: Run full affected suite and independent review**

Run: `pytest -q tests/test_personal_calendar*.py tests/test_personal_obsidian_projection.py tests/test_personal_daily_*.py`
Expected: PASS.

- [ ] **Step 6: Commit safe manifest**

```bash
git add docs/runbooks/personal-calendar.md docs/release-manifests/personal-calendar-obsidian-v1.json
git commit -m "docs(private): record calendar and Obsidian acceptance"
```
