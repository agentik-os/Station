# Station File Intake and Discord Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a fleet-wide, authorization-bound Discord history interface and resumable file intake that accepts exactly 100 GiB without buffering objects in gateway memory.

**Architecture:** A dedicated Tailnet-only `agk-file-intake` service owns upload state, quarantine and object bytes. Hermes exposes typed Discord/history and intake tools; the Discord adapter streams native attachments into the same service and presents a secure-upload button when Discord cannot transport the object.

**Tech Stack:** Python 3.11 stdlib HTTP/SQLite, aiohttp already used by Hermes, discord.py, systemd hardening, Tailscale Serve, pytest.

**Spec:** `docs/superpowers/specs/2026-08-31-station-journal-focus-large-file-design.md`

## Global Constraints

- Maximum accepted object size is exactly `107374182400` bytes; 100 GiB plus one is rejected before transfer.
- Never use `attachment.read()` for the streaming path and never configure zero as unlimited.
- Bind intake to loopback and expose only through authenticated Tailnet HTTPS.
- Tokens, signed URLs, profile paths and secret headers never enter prompts or logs.
- DMs and unregistered Discord channels are denied.
- Keep Linux homes, credentials, sessions and memories isolated.
- Work in a clean worktree; do not include the dirty `hotfix/director-autonomy-20260830` worktree.
- Every task follows RED → GREEN → REFACTOR and ends with an exact-file commit.

## File map

- `overlay/scripts/agk_file_intake/domain.py` — immutable types, states, limits and validation.
- `overlay/scripts/agk_file_intake/store.py` — SQLite transactions and filesystem reservation/finalization.
- `overlay/scripts/agk_file_intake/service.py` — loopback HTTP upload/status API.
- `overlay/scripts/agk_file_intake/scanner.py` — SHA-256 and ClamAV boundary.
- `overlay/scripts/agk_file_intake/capability.py` — HMAC one-shot capabilities.
- `overlay/scripts/agk_file_intake_client.py` — profile-safe client used by Hermes tools.
- `overlay/hermes/plugins/agentik_os/file_intake.py` — Hermes tool schema/handler.
- `overlay/hermes/plugins/platforms/discord/agk_discord_read.py` — typed bounded history reader.
- `overlay/hermes/plugins/platforms/discord/agk_large_file_ui.py` — `Upload large file` view.
- `overlay/hermes/plugins/platforms/discord/adapter.py` — streaming attachment integration only.
- `overlay/systemd/agk-file-intake.service` — hardened service.
- `overlay/config/file-intake-policy.json` — fixed fleet/profile/channel policy.
- `overlay/install.sh` — recoverable installation wiring.
- `tests/test_file_intake_domain.py`, `tests/test_file_intake_service.py`, `tests/test_file_intake_security.py`, `tests/test_discord_read_tool.py`, `tests/test_discord_large_attachment.py`, `tests/test_file_intake_installer.py` — acceptance tests.

---

### Task 1: Domain contract and exact 100 GiB boundary

**Files:**
- Create: `overlay/scripts/agk_file_intake/__init__.py`
- Create: `overlay/scripts/agk_file_intake/domain.py`
- Test: `tests/test_file_intake_domain.py`

**Interfaces:**
- Produces: `MAX_OBJECT_BYTES = 107374182400`, `UploadState`, `UploadSpec`, `ObjectManifest`, `validate_declared_size(size: int) -> int`.

- [ ] **Step 1: Write the failing boundary tests**

```python
from agk_file_intake.domain import MAX_OBJECT_BYTES, validate_declared_size

def test_exact_100_gib_is_accepted():
    assert MAX_OBJECT_BYTES == 107374182400
    assert validate_declared_size(MAX_OBJECT_BYTES) == MAX_OBJECT_BYTES

def test_100_gib_plus_one_is_rejected():
    with pytest.raises(ValueError, match="exceeds 100 GiB"):
        validate_declared_size(107374182401)
```

- [ ] **Step 2: Run the focused test and confirm RED**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_file_intake_domain.py`
Expected: FAIL because `agk_file_intake.domain` does not exist.

- [ ] **Step 3: Implement immutable types and strict integer validation**

```python
MAX_OBJECT_BYTES = 100 * 1024 ** 3
class UploadState(str, Enum):
    CREATED="created"; UPLOADING="uploading"; QUARANTINED="quarantined"
    VERIFYING="verifying"; AVAILABLE="available"; BLOCKED="blocked"

def validate_declared_size(size: int) -> int:
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError("size must be a non-negative integer")
    if size > MAX_OBJECT_BYTES:
        raise ValueError("object exceeds 100 GiB")
    return size
```

- [ ] **Step 4: Run tests and compile**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_file_intake_domain.py && python3 -m py_compile overlay/scripts/agk_file_intake/*.py`
Expected: PASS.

- [ ] **Step 5: Commit exact files**

```bash
git add overlay/scripts/agk_file_intake/__init__.py overlay/scripts/agk_file_intake/domain.py tests/test_file_intake_domain.py
git commit -m "feat(intake): define 100 GiB object contract"
```

### Task 2: Capability tokens and fixed policy

**Files:**
- Create: `overlay/scripts/agk_file_intake/capability.py`
- Create: `overlay/config/file-intake-policy.json`
- Modify: `overlay/scripts/agk_file_intake/domain.py`
- Test: `tests/test_file_intake_security.py`

**Interfaces:**
- Consumes: `UploadSpec` from Task 1.
- Produces: `CapabilityIssuer.issue(spec, verbs, expires_at) -> str`, `verify(token, verb, now) -> CapabilityClaims`.

- [ ] **Step 1: Write failing tests for expiry, verb binding, tampering and cross-profile denial**

```python
token = issuer.issue(spec, verbs=("append","status"), expires_at=200)
assert issuer.verify(token, "append", now=199).upload_id == spec.upload_id
with pytest.raises(PermissionError): issuer.verify(token, "finalize", now=199)
with pytest.raises(PermissionError): issuer.verify(token + "x", "append", now=199)
with pytest.raises(PermissionError): issuer.verify(token, "append", now=201)
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_file_intake_security.py -k capability`
Expected: FAIL because `CapabilityIssuer` is missing.

- [ ] **Step 3: Implement canonical JSON + HMAC-SHA256 tokens**

Use URL-safe base64 without padding, `hmac.compare_digest`, integer expiry, immutable upload/profile/recipient/verb claims, and reject unknown JSON keys. Load the HMAC key from a mode-0600 systemd credential path; never from policy JSON.

- [ ] **Step 4: Add a fixed policy containing only Station profile IDs and approved Discord guild/channel IDs**

```json
{"version":1,"profiles":["operator","agentik","mission","private"],"max_object_bytes":107374182400,"deny_dm":true}
```

- [ ] **Step 5: Run security tests**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_file_intake_security.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/scripts/agk_file_intake/capability.py overlay/scripts/agk_file_intake/domain.py overlay/config/file-intake-policy.json tests/test_file_intake_security.py
git commit -m "feat(intake): bind one-shot upload capabilities"
```

### Task 3: Transactional store and storage reservation

**Files:**
- Create: `overlay/scripts/agk_file_intake/store.py`
- Test: `tests/test_file_intake_service.py`
- Test: `tests/test_file_intake_security.py`

**Interfaces:**
- Produces: `IntakeStore.create(spec)`, `reserve(upload_id)`, `append(upload_id, offset, chunks)`, `begin_finalize(upload_id)`, `mark_available(manifest)`, `get_manifest(object_id)`.

- [ ] **Step 1: Write failing tests for reservation, offset replay, low disk and path safety**

```python
store.create(spec)
store.reserve(spec.upload_id, free_bytes=spec.declared_size + store.free_floor)
assert store.append(spec.upload_id, 0, [b"abc"]) == 3
with pytest.raises(Conflict): store.append(spec.upload_id, 0, [b"abc"])
with pytest.raises(StorageBlocked): store.reserve(other.upload_id, free_bytes=store.free_floor)
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_file_intake_service.py -k store`
Expected: FAIL because `IntakeStore` is missing.

- [ ] **Step 3: Implement SQLite WAL transactions and dirfd-relative object creation**

Use `os.open(..., O_CREAT|O_EXCL|O_NOFOLLOW, 0o600, dir_fd=objects_fd)`, generated hex IDs only, `BEGIN IMMEDIATE`, stored acknowledged offsets, and `os.statvfs` reservation checks. Never join a user filename into a path.

- [ ] **Step 4: Add observed-byte hard stop inside the append loop**

```python
for chunk in chunks:
    if offset + len(chunk) > declared_size or offset + len(chunk) > MAX_OBJECT_BYTES:
        raise SizeMismatch("observed bytes exceed reservation")
    os.write(fd, chunk)
    offset += len(chunk)
```

- [ ] **Step 5: Run store/security tests**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_file_intake_service.py tests/test_file_intake_security.py`
Expected: PASS with no partial file after failed reservation.

- [ ] **Step 6: Commit**

```bash
git add overlay/scripts/agk_file_intake/store.py tests/test_file_intake_service.py tests/test_file_intake_security.py
git commit -m "feat(intake): add transactional resumable store"
```

### Task 4: Finalization, hash and malware gate

**Files:**
- Create: `overlay/scripts/agk_file_intake/scanner.py`
- Modify: `overlay/scripts/agk_file_intake/store.py`
- Test: `tests/test_file_intake_service.py`
- Test: `tests/test_file_intake_security.py`

**Interfaces:**
- Produces: `Scanner.scan(path) -> ScanVerdict`, `IntakeStore.finalize(upload_id, scanner) -> ObjectManifest`.

- [ ] **Step 1: Write failing tests for size mismatch, SHA mismatch, malware and scanner unavailable**

```python
with pytest.raises(HashMismatch): store.finalize(upload_id, CleanScanner(), expected_sha256="00"*32)
assert store.state(upload_id) == UploadState.BLOCKED
with pytest.raises(ScanUnavailable): store.finalize(other_id, OfflineScanner())
assert store.state(other_id) == UploadState.BLOCKED
```

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_file_intake_service.py -k finalize`
Expected: FAIL because finalization is absent.

- [ ] **Step 3: Implement chunked SHA-256, fsync and ClamAV `INSTREAM` client**

Read fixed 8 MiB chunks; send length-prefixed chunks to the local ClamAV socket; treat timeout, malformed response or unavailable socket as blocked. On clean verdict, fsync file and parent directory then atomically rename from quarantine to objects.

- [ ] **Step 4: Run tests and verify blocked objects cannot be opened**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_file_intake_service.py tests/test_file_intake_security.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add overlay/scripts/agk_file_intake/scanner.py overlay/scripts/agk_file_intake/store.py tests/test_file_intake_service.py tests/test_file_intake_security.py
git commit -m "feat(intake): verify and quarantine uploaded objects"
```

### Task 5: Loopback resumable HTTP service and client

**Files:**
- Create: `overlay/scripts/agk_file_intake/service.py`
- Create: `overlay/scripts/agk_file_intake_client.py`
- Test: `tests/test_file_intake_service.py`

**Interfaces:**
- Produces HTTP: `POST /v1/uploads`, `HEAD/PATCH /v1/uploads/{id}`, `POST /v1/uploads/{id}/finalize`, `GET /v1/objects/{id}` metadata only.
- Produces Python: `IntakeClient.open_upload`, `append`, `finalize`, `status`, `manifest`.

- [ ] **Step 1: Write failing protocol tests**

Assert bearer capability verification, `Upload-Offset` conflict returns 409 with authoritative offset, oversized `Content-Length` returns 413 before body read, and responses never echo authorization.

- [ ] **Step 2: Run RED**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_file_intake_service.py -k http`
Expected: FAIL because the service is missing.

- [ ] **Step 3: Implement the minimal asyncio HTTP server and streaming body reader**

The request loop must call `reader.read(min(8 * 1024 * 1024, remaining))`, pass chunks directly to `IntakeStore.append`, set a per-request idle timeout and close on framing errors.

- [ ] **Step 4: Implement the profile client with typed errors and redacted exceptions**

Client exceptions include status, safe error code and upload ID only; signed URL, bearer token and response body are excluded.

- [ ] **Step 5: Run protocol tests**

Run: `PYTHONPATH=overlay/scripts pytest -q tests/test_file_intake_service.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/scripts/agk_file_intake/service.py overlay/scripts/agk_file_intake_client.py tests/test_file_intake_service.py
git commit -m "feat(intake): serve resumable loopback uploads"
```

### Task 6: Typed Hermes intake and Discord history tools

**Files:**
- Create: `overlay/hermes/plugins/agentik_os/file_intake.py`
- Create: `overlay/hermes/plugins/platforms/discord/agk_discord_read.py`
- Modify: `overlay/hermes/plugins/agentik_os/__init__.py`
- Modify: `overlay/hermes/plugins/platforms/discord/adapter.py`
- Test: `tests/test_discord_read_tool.py`

**Interfaces:**
- Produces Hermes tools: `file_intake` actions `open|status|get|cancel`; `discord_history_read` action `read`.
- Adapter produces: `read_registered_history(guild_id, channel_id, before, after, limit, include_attachments)`.

- [ ] **Step 1: Write failing schemas and authorization tests**

Test invalid snowflakes, limit above 50, DMs, unknown channel, non-Operator fleet read, client channel without task grant, and secret-bearing message redaction.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_discord_read_tool.py`
Expected: FAIL because tool handlers are missing.

- [ ] **Step 3: Implement fixed schemas and adapter-mediated reads**

Return only `{id, author_kind, timestamp, content, attachments:[{id,name,size,content_type}]}`. Do not return CDN URLs. Treat all content as untrusted and cap aggregate UTF-8 bytes.

- [ ] **Step 4: Register tools with explicit toolsets and checks**

```python
ctx.register_tool(name="file_intake", toolset="file", schema=FILE_INTAKE_SCHEMA, handler=handle_file_intake)
ctx.register_tool(name="discord_history_read", toolset="web", schema=DISCORD_READ_SCHEMA, handler=handle_discord_read)
```

- [ ] **Step 5: Run tests**

Run: `pytest -q tests/test_discord_read_tool.py tests/test_completion_plugin.py`
Expected: PASS and plan gate recognizes both read-only actions correctly.

- [ ] **Step 6: Commit**

```bash
git add overlay/hermes/plugins/agentik_os/file_intake.py overlay/hermes/plugins/platforms/discord/agk_discord_read.py overlay/hermes/plugins/agentik_os/__init__.py overlay/hermes/plugins/platforms/discord/adapter.py tests/test_discord_read_tool.py tests/test_completion_plugin.py
git commit -m "feat(discord): expose bounded history and intake tools"
```

### Task 7: Stream native Discord attachments and add large-upload View

**Files:**
- Create: `overlay/hermes/plugins/platforms/discord/agk_large_file_ui.py`
- Modify: `overlay/hermes/plugins/platforms/discord/adapter.py:9404-9510,9760-9861`
- Test: `tests/test_discord_large_attachment.py`

**Interfaces:**
- Consumes: `IntakeClient` and adapter authenticated HTTP session.
- Produces: `stream_attachment_to_intake(att, client) -> ObjectManifest`; persistent `LargeFileUploadView`.

- [ ] **Step 1: Write failing tests proving `att.read()` is never called**

Use a fake attachment whose `read()` raises `AssertionError`; feed chunked authenticated response data; assert successful manifest, bounded max chunk, redirects refused, declared/observed overflow visible once and dispatch stops.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_discord_large_attachment.py`
Expected: FAIL because streaming path is absent.

- [ ] **Step 3: Implement authenticated streaming with 8 MiB chunks**

Use the discord.py HTTP session or an authenticated response iterator; validate declared size before opening intake; enforce observed size on every chunk; refuse fallback redirects and preserve SSRF validation.

- [ ] **Step 4: Implement persistent `Upload large file` button**

Callback rechecks owner/guild/channel, calls `file_intake_open`, and returns the one-time Tailnet URL ephemerally. Never put the URL in logs or persistent message content.

- [ ] **Step 5: Run attachment and existing Discord regression tests**

Run: `pytest -q tests/test_discord_large_attachment.py tests/test_discord_reply_formatting.py tests/test_discord_compression_noise.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/hermes/plugins/platforms/discord/agk_large_file_ui.py overlay/hermes/plugins/platforms/discord/adapter.py tests/test_discord_large_attachment.py
git commit -m "feat(discord): stream attachments into fleet intake"
```

### Task 8: Hardened installation, health and rollback

**Files:**
- Create: `overlay/systemd/agk-file-intake.service`
- Create: `overlay/scripts/install-agk-file-intake.py`
- Modify: `overlay/install.sh`
- Test: `tests/test_file_intake_installer.py`
- Modify: `README.md`

**Interfaces:**
- Produces CLI: `install-agk-file-intake.py install|check|rollback --release <path>`.
- Produces health: loopback `GET /healthz` with state, disk floor and scanner status only.

- [ ] **Step 1: Write failing installer contract tests**

Assert dedicated user, `ProtectSystem=strict`, `ProtectHome=true`, `NoNewPrivileges=true`, `PrivateTmp=true`, explicit `ReadWritePaths=/var/lib/agk-file-intake`, loopback binding, mode-0600 credential, backup manifest and allowlisted rollback target.

- [ ] **Step 2: Run RED**

Run: `pytest -q tests/test_file_intake_installer.py`
Expected: FAIL because artifacts are missing.

- [ ] **Step 3: Implement transactional installer and unit**

Installer preflights UID, volume, ClamAV socket and Tailscale; writes a unique backup; installs to a versioned directory; atomically switches a symlink; daemon-reloads; starts paused; health-checks; compensates on failure.

- [ ] **Step 4: Wire exact files in `overlay/install.sh` without starting gateways**

Install package, client, policy, unit and installer. Enabling intake is a separate explicit system-install action after tests.

- [ ] **Step 5: Run installer and Station contract tests**

Run: `pytest -q tests/test_file_intake_installer.py tests/test_station_contract.py`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add overlay/systemd/agk-file-intake.service overlay/scripts/install-agk-file-intake.py overlay/install.sh tests/test_file_intake_installer.py README.md
git commit -m "feat(intake): install hardened recoverable service"
```

### Task 9: Real 100 GiB canary and adversarial acceptance

**Files:**
- Create: `overlay/scripts/verify-agk-file-intake.py`
- Modify: `tests/test_file_intake_security.py`
- Create: `docs/runbooks/file-intake.md`

**Interfaces:**
- Produces CLI: `verify-agk-file-intake.py --size 107374182400 --resume-at 53687091200 --cleanup` returning JSON receipt.

- [ ] **Step 1: Add failing receipt-schema tests**

Require `{declared_bytes, observed_bytes, sha256, peak_rss_bytes, resumed, state, cleanup_free_bytes_delta}` and fail if exact size/hash/readback is absent.

- [ ] **Step 2: Implement a deterministic zero-stream canary with precomputed SHA-256 derived during the run**

Generate fixed 8 MiB zero chunks, interrupt once at the requested offset, restart client, resume from authoritative offset, finalize, read back the manifest, sample service RSS, then delete through the approved cleanup path.

- [ ] **Step 3: Run all offline tests**

Run: `pytest -q tests/test_file_intake_*.py tests/test_discord_read_tool.py tests/test_discord_large_attachment.py`
Expected: PASS.

- [ ] **Step 4: Deploy staged service and run the actual 100 GiB canary**

Run: `sudo /usr/local/lib/agk-terminal/scripts/verify-agk-file-intake.py --size 107374182400 --resume-at 53687091200 --cleanup`
Expected: exit 0; exact 100 GiB available before cleanup; RSS remains below the explicit service memory limit; free space returns within filesystem-accounting tolerance.

- [ ] **Step 5: Exercise live Discord read and small attachment interactions**

Use registered non-client channels, click the real upload View, verify native readback and ensure no signed URL/token appears in gateway logs.

- [ ] **Step 6: Commit evidence-safe runbook and verifier**

```bash
git add overlay/scripts/verify-agk-file-intake.py tests/test_file_intake_security.py docs/runbooks/file-intake.md
git commit -m "test(intake): prove resumable 100 GiB delivery"
```

### Task 10: Owner gates, fleet rollout and independent review

**Files:**
- Modify: `docs/runbooks/file-intake.md`
- Create: `docs/release-manifests/file-intake-v1.json`

**Interfaces:**
- Consumes accepted Tasks 1–9.
- Produces deployed checksum manifest and redacted fleet acceptance receipt.

- [ ] **Step 1: Have Gareth authorize the Operator bot installation and rotate its token in the Discord Developer Portal**

Use Station secure token onboarding; never accept the token in Discord or CLI arguments. Validate bot identity through a read-only request.

- [ ] **Step 2: Quarantine the obsolete mode-0600 resume credential file recoverably**

After token rotation and live validation, move it into a root-only encrypted backup, scan for unapproved copies by filename/content hash without printing values, then remove the plaintext source.

- [ ] **Step 3: Pilot Operator and Private**

Install the staged shared release, reload only those gateways after drain, verify tools, real component click, native history read and attachment intake.

- [ ] **Step 4: Expand to Agentik and Mission transactionally**

Verify profile-local authorization and deny cross-profile object access without a fleet capability.

- [ ] **Step 5: Request independent security review**

Reviewer checks current diff, service unit, live listeners, capability abuse, path attacks, memory/disk bounds, secret scans and rollback. Resolve every blocking finding before activation.

- [ ] **Step 6: Record checksums and final live readbacks**

Manifest records source commit, installed checksums, service UID, loopback/Tailnet listeners, 100 GiB receipt, four profile tool availability and rollback target. Do not include internal signed URLs or credentials.

- [ ] **Step 7: Commit release manifest**

```bash
git add docs/runbooks/file-intake.md docs/release-manifests/file-intake-v1.json
git commit -m "docs(intake): record fleet acceptance and rollback"
```
