# Private Daily OS and Focus Timer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver restart-safe Morning Planner, FOCUS TIMER and Evening Review interactions owned by Private.

**Architecture:** A focused `personal_daily` package inside the shared Agentik OS plugin owns a profile-local SQLite database and pure domain transitions. Discord Views project canonical state and every callback rechecks Gareth, guild, channel and immutable target.

**Tech Stack:** Python 3.11, SQLite WAL, discord.py persistent Views, Hermes cron, pytest.

**Spec:** `docs/superpowers/specs/2026-08-31-station-journal-focus-large-file-design.md`

## Global Constraints

- Private owns all personal mutable state; Operator does not read Private sessions or credentials.
- Morning channel is `1542490950515953774`; Evening channel is `1542490943272259625`.
- Schedule is 08:00 and 21:00 Europe/Paris.
- Create one dedicated `FOCUS TIMER` channel under the existing Personal OS category resolved from live state.
- One active task maximum; elapsed time derives from persisted UTC timestamps and excludes pauses.
- No automatic threads and no duplicate daily Views.
- One gentle missed-response reminder maximum.
- Every action uses a real dynamic `discord.ui.View` and live E2E click verification.

## File map

- `overlay/hermes/plugins/agentik_os/personal_daily/domain.py` — tasks, timers, reviews and transitions.
- `overlay/hermes/plugins/agentik_os/personal_daily/store.py` — profile-local SQLite and migrations.
- `overlay/hermes/plugins/agentik_os/personal_daily/render.py` — compact Station copy.
- `overlay/hermes/plugins/platforms/discord/agk_personal_daily_ui.py` — persistent Views and callbacks.
- `overlay/scripts/personal_daily_cron.py` — deterministic morning/evening/recovery entry point.
- `overlay/scripts/install-personal-daily.py` — channel resolution, cron update, rollback.
- `tests/test_personal_daily_domain.py`, `tests/test_personal_daily_store.py`, `tests/test_personal_daily_ui.py`, `tests/test_personal_daily_cron.py`, `tests/test_personal_daily_installer.py`.

---

### Task 1: Task and review domain

**Files:**
- Create: `overlay/hermes/plugins/agentik_os/personal_daily/__init__.py`
- Create: `overlay/hermes/plugins/agentik_os/personal_daily/domain.py`
- Test: `tests/test_personal_daily_domain.py`

**Interfaces:**
- Produces: `TaskCategory`, `TaskStatus`, `DailyTask`, `EveningReview`, `create_task`, `rank_tasks`, `complete_task`, `carry_task`.

- [ ] **Step 1: Write failing invariants tests**

```python
task = create_task(day=date(2026,8,31), title="Ship intake", category="mit", blocks=2)
assert task.estimated_seconds == 3600
with pytest.raises(ValueError): create_task(day=task.day, title="", category="mit", blocks=1)
with pytest.raises(ValueError): create_task(day=task.day, title="x", category="mit", blocks=0)
```

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_daily_domain.py`
Expected: FAIL because package is missing.

- [ ] **Step 3: Implement frozen dataclasses and pure transitions**

Use UUIDv4 hex IDs, non-empty titles capped at 240 characters, block count `1..48`, explicit category/status enums and immutable replacement transitions.

- [ ] **Step 4: Run tests**

Run: `pytest -q tests/test_personal_daily_domain.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add overlay/hermes/plugins/agentik_os/personal_daily tests/test_personal_daily_domain.py
git commit -m "feat(private): define daily planning domain"
```

### Task 2: Restart-safe timer state machine

**Files:**
- Modify: `overlay/hermes/plugins/agentik_os/personal_daily/domain.py`
- Test: `tests/test_personal_daily_domain.py`

**Interfaces:**
- Produces: `FocusTimer.start(task_id, now)`, `pause(now)`, `resume(now)`, `extend(blocks)`, `finish(now)`, `active_seconds(now)`.

- [ ] **Step 1: Write failing tests with a fake clock**

```python
timer = FocusTimer.start("task-1", at(8,0))
timer = timer.pause(at(8,20)).resume(at(8,35)).finish(at(9,5))
assert timer.active_seconds(at(9,5)) == 50 * 60
assert timer.actual_blocks == 2
```

Also assert starting a second task while one is active raises `ActiveTimerConflict`.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_daily_domain.py -k timer`
Expected: FAIL.

- [ ] **Step 3: Implement timestamp-derived accounting**

Store `started_at`, completed pause intervals and optional open pause. Compute active time from intervals; do not increment counters in background.

- [ ] **Step 4: Run tests**

Run: `pytest -q tests/test_personal_daily_domain.py`
Expected: PASS across DST-independent UTC timestamps.

- [ ] **Step 5: Commit**

```bash
git add overlay/hermes/plugins/agentik_os/personal_daily/domain.py tests/test_personal_daily_domain.py
git commit -m "feat(private): add restart-safe focus timer"
```

### Task 3: Profile-local SQLite store and migrations

**Files:**
- Create: `overlay/hermes/plugins/agentik_os/personal_daily/store.py`
- Test: `tests/test_personal_daily_store.py`

**Interfaces:**
- Produces: `DailyStore.open(hermes_home)`, `create_task`, `list_day`, `transition_timer`, `save_review`, `claim_idempotency_key`, `set_view_receipt`.

- [ ] **Step 1: Write failing transaction/restart tests**

Open two store instances on one temporary database, assert WAL mode, one active timer unique index, atomic idempotency claims and state equality after process-style reopen.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_daily_store.py`
Expected: FAIL because store is missing.

- [ ] **Step 3: Implement schema version 1 and repository methods**

Tables: `tasks`, `timers`, `timer_pauses`, `reviews`, `habits`, `view_receipts`, `idempotency`. Use foreign keys, `BEGIN IMMEDIATE`, ISO UTC timestamps and a partial unique index for the active timer.

- [ ] **Step 4: Add migration backup and refusal tests**

Migration creates a mode-0600 database backup first; a future schema version fails closed without mutation.

- [ ] **Step 5: Run tests and compile**

Run: `pytest -q tests/test_personal_daily_store.py && python3 -m py_compile overlay/hermes/plugins/agentik_os/personal_daily/*.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/hermes/plugins/agentik_os/personal_daily/store.py tests/test_personal_daily_store.py
git commit -m "feat(private): persist daily OS state"
```

### Task 4: Compact renderers

**Files:**
- Create: `overlay/hermes/plugins/agentik_os/personal_daily/render.py`
- Test: `tests/test_personal_daily_ui.py`

**Interfaces:**
- Produces: `render_morning(day_state)`, `render_focus(timer, task, now)`, `render_evening(review_state)`.

- [ ] **Step 1: Write failing copy/length tests**

Assert monochrome headings, MIT/Secondary/Additional grouping, planned/actual blocks, no gradients/icons/card wall, no internal IDs/paths and content below 1900 characters.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_daily_ui.py -k render`
Expected: FAIL.

- [ ] **Step 3: Implement deterministic renderers**

Use terse editorial copy and whole metric variants such as `2 × 30 min planned · 47 min actual`.

- [ ] **Step 4: Run tests**

Run: `pytest -q tests/test_personal_daily_ui.py -k render`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add overlay/hermes/plugins/agentik_os/personal_daily/render.py tests/test_personal_daily_ui.py
git commit -m "feat(private): render daily planning surfaces"
```

### Task 5: Morning Planner persistent View

**Files:**
- Create: `overlay/hermes/plugins/platforms/discord/agk_personal_daily_ui.py`
- Modify: `overlay/hermes/plugins/platforms/discord/adapter.py`
- Test: `tests/test_personal_daily_ui.py`

**Interfaces:**
- Produces: `MorningPlannerView`, `register_personal_daily_views(adapter)`, `ensure_morning_view(adapter, day)`.

- [ ] **Step 1: Write failing callback authorization and dedupe tests**

Fake owner succeeds; wrong user/guild/channel is denied ephemerally. Two `ensure_morning_view` calls for one Paris-local date edit one message rather than send two.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_daily_ui.py -k morning`
Expected: FAIL.

- [ ] **Step 3: Implement selects/buttons/modals**

Use selects for category/rank/blocks, modal only for task title or note, buttons `Add`, `Reorder`, `Schedule`, `Start`, `Done`, `Carry`, `Plan my day`. Persist message ID before considering the view delivered.

- [ ] **Step 4: Register persistent Views during Discord ready**

Rehydrate by saved message ID and immutable custom IDs containing only signed short action/task references.

- [ ] **Step 5: Run UI and Discord regression tests**

Run: `pytest -q tests/test_personal_daily_ui.py tests/test_agk_discord_ui_policy.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/hermes/plugins/platforms/discord/agk_personal_daily_ui.py overlay/hermes/plugins/platforms/discord/adapter.py tests/test_personal_daily_ui.py
git commit -m "feat(private): add interactive morning planner"
```

### Task 6: FOCUS TIMER channel and View

**Files:**
- Modify: `overlay/hermes/plugins/platforms/discord/agk_personal_daily_ui.py`
- Modify: `overlay/hermes/plugins/agentik_os/personal_daily/render.py`
- Test: `tests/test_personal_daily_ui.py`

**Interfaces:**
- Produces: `FocusTimerView`, `ensure_focus_channel`, `ensure_focus_view`, `emit_block_checkpoint`.

- [ ] **Step 1: Write failing creation and one-active-task tests**

Assert exact category parent is discovered before create; existing channel is reused; default-role denied; owner and Private bot allowed; second `Start` receives conflict; restart renders correct elapsed time.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_daily_ui.py -k focus`
Expected: FAIL.

- [ ] **Step 3: Implement channel creation and controls**

Controls: `Start`, `Pause`, `Resume`, `Done`, `+30 min`, `Replan`, `Stop`. Every callback opens a fresh transaction, reads canonical state, authorizes, transitions and edits the single view.

- [ ] **Step 4: Implement one checkpoint per estimated boundary**

Persist boundary number in `idempotency`; emit `Done`, `+30 min`, `Replan`; never stack multiple unresolved checkpoint messages.

- [ ] **Step 5: Run tests**

Run: `pytest -q tests/test_personal_daily_ui.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/hermes/plugins/platforms/discord/agk_personal_daily_ui.py overlay/hermes/plugins/agentik_os/personal_daily/render.py tests/test_personal_daily_ui.py
git commit -m "feat(private): add persistent focus timer"
```

### Task 7: Evening Review and habits

**Files:**
- Modify: `overlay/hermes/plugins/platforms/discord/agk_personal_daily_ui.py`
- Modify: `overlay/hermes/plugins/agentik_os/personal_daily/domain.py`
- Modify: `overlay/hermes/plugins/agentik_os/personal_daily/store.py`
- Test: `tests/test_personal_daily_ui.py`
- Test: `tests/test_personal_daily_store.py`

**Interfaces:**
- Produces: `EveningReviewView`, habit toggles, review finalization and `journal_safe` field classification.

- [ ] **Step 1: Write failing required-field and privacy tests**

Require highlight, learning, memory, identity today/tomorrow and task outcomes. Habit notes are optional. No field becomes `journal_safe` without explicit owner toggle.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_daily_ui.py -k evening tests/test_personal_daily_store.py -k review`
Expected: FAIL.

- [ ] **Step 3: Implement progressive-disclosure review View**

Use one select for task outcomes, one habit selector, and bounded modals for reflection text. `Save draft` is distinct from `Complete review`.

- [ ] **Step 4: Run tests**

Run: `pytest -q tests/test_personal_daily_ui.py tests/test_personal_daily_store.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add overlay/hermes/plugins/platforms/discord/agk_personal_daily_ui.py overlay/hermes/plugins/agentik_os/personal_daily/domain.py overlay/hermes/plugins/agentik_os/personal_daily/store.py tests/test_personal_daily_ui.py tests/test_personal_daily_store.py
git commit -m "feat(private): add evening review and habits"
```

### Task 8: Cron runner, recovery and transactional install

**Files:**
- Create: `overlay/scripts/personal_daily_cron.py`
- Create: `overlay/scripts/install-personal-daily.py`
- Modify: `overlay/install.sh`
- Test: `tests/test_personal_daily_cron.py`
- Test: `tests/test_personal_daily_installer.py`

**Interfaces:**
- Runner: `personal_daily_cron.py morning|checkpoint|evening|recover --date YYYY-MM-DD`.
- Installer: `install-personal-daily.py check|install|rollback`.

- [ ] **Step 1: Write failing idempotency and timezone tests**

Use Paris DST fixtures, two concurrent morning runs, gateway restart with active timer, one reminder only, and correction of Evening delivery to `1542490943272259625`.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_personal_daily_cron.py tests/test_personal_daily_installer.py`
Expected: FAIL.

- [ ] **Step 3: Implement deterministic runner**

Runner reads the requested local date, claims `job_kind:date`, sends/edits through adapter state and exits with structured JSON. Healthy/no-op output is empty for scheduled delivery.

- [ ] **Step 4: Implement installer using `hermes cron update` semantics**

Back up current Private cron definitions, update existing Morning and Evening jobs rather than create duplicates, set Europe/Paris schedules, install workdir and recovery metadata, then read back exact job IDs/schedules/delivery.

- [ ] **Step 5: Wire scripts into shared installer**

Do not start or reload any gateway during package installation.

- [ ] **Step 6: Run tests**

Run: `pytest -q tests/test_personal_daily_*.py tests/test_station_contract.py`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add overlay/scripts/personal_daily_cron.py overlay/scripts/install-personal-daily.py overlay/install.sh tests/test_personal_daily_cron.py tests/test_personal_daily_installer.py
git commit -m "feat(private): schedule recoverable daily OS"
```

### Task 9: Live Private E2E and rollback proof

**Files:**
- Create: `docs/runbooks/personal-daily-os.md`
- Create: `docs/release-manifests/personal-daily-os-v1.json`

**Interfaces:**
- Produces safe E2E receipt for channel IDs, message IDs, interaction results, database schema, cron readbacks and rollback target.

- [ ] **Step 1: Deploy to a staged shared Hermes release and run offline suite**

Run: `pytest -q tests/test_personal_daily_*.py tests/test_agk_discord_ui_policy.py`
Expected: PASS.

- [ ] **Step 2: Install Private state and Views with recurring jobs paused**

Read back database owner/mode, exact channel parent/overwrites, view receipts and cron definitions.

- [ ] **Step 3: Perform real component clicks**

Create one MIT with two blocks, start, pause, resume, extend, finish, complete review and recover the same state after a controlled Private-only drain-safe reload.

- [ ] **Step 4: Verify no duplicates or channel/thread noise**

Assert one morning message, one focus surface, one evening message, no auto-thread, and one reminder maximum.

- [ ] **Step 5: Exercise rollback and restore**

Rollback package/schema/jobs, verify previous crons restored, then reapply release and confirm task/timer history survives.

- [ ] **Step 6: Independent review and commit receipt**

```bash
git add docs/runbooks/personal-daily-os.md docs/release-manifests/personal-daily-os-v1.json
git commit -m "docs(private): record daily OS live acceptance"
```
