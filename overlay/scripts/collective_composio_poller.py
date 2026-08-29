#!/usr/bin/env python3
"""Poll Composio-backed Stripe/Typeform and apply verified Discord effects."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from collective_automation_core import (
    CollectiveStore,
    map_deal_response,
    map_intro_response,
    map_paid_checkout,
    payload_hash,
)

API = "https://discord.com/api/v10"
GUILD_ID = "1350170767366688830"
INTRO_FORM = "xdQWd8Gv"
DEAL_FORM = "r7DHpFxv"
INTRO_CHANNEL = "1541214002871406763"
DEALS_CHANNEL = "1541215608920612965"
PRO_ROLE = "1541213916640841859"
FREE_ROLE = "1541213914002489374"
COMPOSIO = "/usr/local/bin/composio"
COMPOSIO_ARTIFACT_ROOTS = (Path("/tmp/composio"), Path("/home/mission/.composio"))
COMPOSIO_ARTIFACT_MAX_BYTES = 20 * 1024 * 1024


def profile_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "/home/mission/.hermes/profiles/collective")


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def decode_composio_result(value: Any) -> Any:
    if not isinstance(value, dict) or not value.get("storedInFile"):
        return value
    raw_path = value.get("outputFilePath")
    if not isinstance(raw_path, str) or not raw_path:
        raise RuntimeError("Composio file output has no path")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise RuntimeError("Composio file output path is not absolute")
    selected: tuple[Path, tuple[str, ...]] | None = None
    for root in COMPOSIO_ARTIFACT_ROOTS:
        canonical_root = Path(os.path.abspath(root))
        try:
            relative = candidate.relative_to(canonical_root)
        except ValueError:
            continue
        if relative.parts and ".." not in relative.parts:
            selected = (canonical_root, relative.parts)
            break
    if selected is None:
        raise RuntimeError("Composio file output escaped its artifact roots")
    root, parts = selected
    directory_fds: list[int] = []
    file_fd: int | None = None
    owned_regular = False
    opened_metadata = None
    parent_fd = None
    final_name = parts[-1]
    flags_directory = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        parent_fd = os.open(root, flags_directory)
        directory_fds.append(parent_fd)
        for component in parts[:-1]:
            parent_fd = os.open(component, flags_directory, dir_fd=parent_fd)
            directory_fds.append(parent_fd)
        file_fd = os.open(final_name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        opened_metadata = os.fstat(file_fd)
        if not stat.S_ISREG(opened_metadata.st_mode) or opened_metadata.st_uid != os.getuid():
            raise RuntimeError("Composio file output has unsafe ownership or type")
        owned_regular = True
        chunks: list[bytes] = []
        remaining = COMPOSIO_ARTIFACT_MAX_BYTES + 1
        while remaining:
            chunk = os.read(file_fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > COMPOSIO_ARTIFACT_MAX_BYTES:
            raise RuntimeError("Composio file output exceeds the size limit")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise RuntimeError("Composio file output returned invalid JSON") from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        if owned_regular and parent_fd is not None and opened_metadata is not None:
            try:
                current = os.stat(final_name, dir_fd=parent_fd, follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (opened_metadata.st_dev, opened_metadata.st_ino):
                    os.unlink(final_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        for directory_fd in reversed(directory_fds):
            os.close(directory_fd)


def composio_execute(slug: str, data: dict[str, Any], timeout: int = 60) -> Any:
    result = subprocess.run(
        [COMPOSIO, "execute", slug, "-d", json.dumps(data, separators=(",", ":"))],
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
        env={**os.environ, "HOME": "/home/mission"},
    )
    if result.returncode:
        raise RuntimeError(f"Composio {slug} failed with exit code {result.returncode}")
    try:
        value = decode_composio_result(json.loads(result.stdout))
    except ValueError as error:
        raise RuntimeError(f"Composio {slug} returned invalid JSON") from error
    if isinstance(value, dict) and value.get("successful") is False:
        raise RuntimeError(f"Composio {slug} reported failure")
    if isinstance(value, dict):
        return value.get("data", value.get("result", value))
    return value


def typeform_responses(form_id: str) -> list[dict[str, Any]]:
    data = composio_execute(
        "TYPEFORM_GET_FORM_RESPONSES",
        {"form_id": form_id, "page_size": 1000, "response_type": ["completed"]},
    )
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise RuntimeError("Typeform response payload has no items list")
    page_count = int(data.get("page_count") or 0) if isinstance(data, dict) else 0
    if page_count > 1:
        raise RuntimeError("Typeform responses exceed one 1000-item page; bounded poll refuses truncation")
    return [item for item in items if isinstance(item, dict)]


def stripe_sessions() -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    after = ""
    for _ in range(20):
        query: dict[str, Any] = {"limit": 100}
        if after:
            query["starting_after"] = after
        data = composio_execute("STRIPE_LIST_CHECKOUT_SESSIONS", query)
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        page = data.get("data") if isinstance(data, dict) else None
        if not isinstance(page, list):
            raise RuntimeError("Stripe checkout payload has no data list")
        clean = [item for item in page if isinstance(item, dict)]
        sessions.extend(clean)
        if not data.get("has_more"):
            return sessions
        if not clean or not clean[-1].get("id"):
            raise RuntimeError("Stripe pagination cannot advance safely")
        after = str(clean[-1]["id"])
    raise RuntimeError("Stripe checkout pagination exceeded safety limit")


def stripe_line_items(session_id: str) -> list[dict[str, Any]]:
    data = composio_execute(
        "STRIPE_GET_CHECKOUT_SESSIONS_SESSION_LINE_ITEMS",
        {"session": session_id, "limit": 100},
    )
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list) or data.get("has_more"):
        raise RuntimeError("Stripe checkout line items are missing or truncated")
    return [item for item in items if isinstance(item, dict)]


def mapped_checkout(session: dict[str, Any]):
    if session.get("payment_status") != "paid" or session.get("status") != "complete":
        return None
    session_id = str(session.get("id") or "")
    if not session_id:
        return None
    return map_paid_checkout(session, stripe_line_items(session_id))


def stripe_event_id(session: dict[str, Any]) -> str | None:
    session_id = str(session.get("id") or "")
    return f"stripe:checkout:{session_id}" if session_id else None


def terminally_ignorable_stripe(session: dict[str, Any]) -> bool:
    return str(session.get("status") or "") in {"complete", "expired"}


class DiscordREST:
    def __init__(self, token: str):
        if not token:
            raise RuntimeError("Collective Discord credential unavailable")
        self.headers = {"Authorization": "Bot " + token, "User-Agent": "DiscordBot (https://github.com/agentik-os, 1.0)"}

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = dict(self.headers)
        if data is not None:
            headers["Content-Type"] = "application/json"
        for attempt in range(6):
            try:
                request = urllib.request.Request(API + path, data=data, method=method, headers=headers)
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
                    return json.loads(raw.decode() or "{}") if raw else {}
            except urllib.error.HTTPError as error:
                raw = error.read().decode("utf-8", "replace")
                if error.code == 429 and attempt < 5:
                    try:
                        delay = float(json.loads(raw).get("retry_after", 1))
                    except Exception:
                        delay = 1
                    time.sleep(min(max(delay, 0.1), 30))
                    continue
                raise RuntimeError(f"Discord {method} {path} failed HTTP {error.code}") from error
        raise RuntimeError("Discord retry budget exhausted")

    def post_and_verify(self, channel_id: str, content: str) -> str:
        existing = self.request("GET", f"/channels/{channel_id}/messages?limit=100")
        if isinstance(existing, list):
            matches = [str(item.get("id")) for item in existing if isinstance(item, dict) and item.get("content") == content]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise RuntimeError("Discord already contains duplicate automation posts")
        created = self.request("POST", f"/channels/{channel_id}/messages", {"content": content, "allowed_mentions": {"parse": []}})
        message_id = str(created.get("id") or "")
        if not message_id:
            raise RuntimeError("Discord post returned no message ID")
        reread = self.request("GET", f"/channels/{channel_id}/messages/{message_id}")
        if reread.get("content") != content or str(reread.get("channel_id")) != channel_id:
            raise RuntimeError("Discord post readback mismatch")
        return message_id

    def grant_pro(self, discord_id: str) -> None:
        self.request("PUT", f"/guilds/{GUILD_ID}/members/{discord_id}/roles/{PRO_ROLE}", {})
        self.request("DELETE", f"/guilds/{GUILD_ID}/members/{discord_id}/roles/{FREE_ROLE}")
        member = self.request("GET", f"/guilds/{GUILD_ID}/members/{discord_id}")
        roles = {str(role) for role in member.get("roles") or []}
        if PRO_ROLE not in roles or FREE_ROLE in roles:
            raise RuntimeError("Discord Pro role readback mismatch")

    def grant_signed(self, discord_id: str) -> bool:
        try:
            self.request("PUT", f"/guilds/{GUILD_ID}/members/{discord_id}/roles/1541225509940109322", {})
            member = self.request("GET", f"/guilds/{GUILD_ID}/members/{discord_id}")
        except RuntimeError as error:
            if "HTTP 404" in str(error):
                return False
            raise
        if "1541225509940109322" not in {str(role) for role in member.get("roles") or []}:
            raise RuntimeError("Discord Signed role readback mismatch")
        return True

    def remove_signed(self, discord_id: str) -> bool:
        try:
            self.request("DELETE", f"/guilds/{GUILD_ID}/members/{discord_id}/roles/1541225509940109322")
            member = self.request("GET", f"/guilds/{GUILD_ID}/members/{discord_id}")
        except RuntimeError as error:
            if "HTTP 404" in str(error):
                return False
            raise
        if "1541225509940109322" in {str(role) for role in member.get("roles") or []}:
            raise RuntimeError("Discord Signed role removal readback mismatch")
        return True

    def signed_member_ids(self) -> set[str]:
        result: set[str] = set()
        after = ""
        for _ in range(20):
            path = f"/guilds/{GUILD_ID}/members?limit=1000"
            if after:
                path += "&after=" + after
            members = self.request("GET", path)
            if not isinstance(members, list):
                raise RuntimeError("Discord member inventory is invalid")
            for member in members:
                if isinstance(member, dict) and "1541225509940109322" in {str(role) for role in member.get("roles") or []}:
                    user_id = str((member.get("user") or {}).get("id") or "")
                    if user_id:
                        result.add(user_id)
            if len(members) < 1000:
                return result
            after = str((members[-1].get("user") or {}).get("id") or "")
            if not after:
                raise RuntimeError("Discord member pagination cannot advance")
        raise RuntimeError("Discord member pagination exceeded safety limit")


def baseline(store: CollectiveStore, discord: DiscordREST, intros: list[dict[str, Any]], deals: list[dict[str, Any]], sessions: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"intros": 0, "deals": 0, "stripe": 0, "legacy_signed": 0}
    counts["legacy_signed"] = store.protect_legacy_signed(discord.signed_member_ids())
    for kind, rows, mapper in (("intros", intros, map_intro_response), ("deals", deals, map_deal_response)):
        for row in rows:
            mapped = mapper(row)
            if store.claim_event(mapped["event_id"], "baseline_" + kind, mapped["payload_hash"]):
                store.mark_delivered(mapped["event_id"], "baseline", "baseline")
                counts[kind] += 1
    for row in sessions:
        event_id = stripe_event_id(row)
        if not event_id:
            continue
        mapped = mapped_checkout(row)
        if mapped:
            digest = mapped["payload_hash"]
            kind = "baseline_stripe"
        elif terminally_ignorable_stripe(row):
            digest = payload_hash({"session_id": event_id, "eligible": False})
            kind = "baseline_stripe_ignored"
        else:
            continue
        if store.claim_event(event_id, kind, digest):
            store.mark_delivered(event_id, "baseline", "baseline")
            if mapped:
                counts["stripe"] += 1
    return counts


def run(initialize: bool = False, dry_run: bool = False) -> dict[str, Any]:
    home = profile_home()
    intros = typeform_responses(INTRO_FORM)
    deals = typeform_responses(DEAL_FORM)
    sessions = stripe_sessions()
    marker = home / "collective-automation-initialized.json"
    if dry_run and not marker.exists():
        return {"mode": "would_initialize", "intros": len(intros), "deals": len(deals), "stripe_sessions": len(sessions), "dry_run": True}
    store = CollectiveStore(home / "collective-automation.db")
    env = load_env(home / ".env")
    discord = DiscordREST(env.get("DISCORD_BOT_TOKEN") or env.get("DISCORD_TOKEN") or "")
    if initialize or not marker.exists():
        counts = baseline(store, discord, intros, deals, sessions)
        marker.write_text(json.dumps({"initialized": True, "counts": counts}, sort_keys=True) + "\n", encoding="utf-8")
        marker.chmod(0o600)
        return {"mode": "initialized", **counts}
    result = {"intros": 0, "deals": 0, "stripe": 0, "signed_reconciled": 0, "signed_removed": 0, "signed_absent": 0, "dry_run": dry_run}
    for kind, rows, mapper, channel in (
        ("intros", intros, map_intro_response, INTRO_CHANNEL),
        ("deals", deals, map_deal_response, DEALS_CHANNEL),
    ):
        for row in rows:
            mapped = mapper(row)
            if dry_run:
                existing = store.event_status(mapped["event_id"])
                if not existing or existing["status"] == "failed":
                    result[kind] += 1
                continue
            if not store.claim_event(mapped["event_id"], kind, mapped["payload_hash"]):
                continue
            try:
                if kind == "deals" and not mapped.get("accepted_split"):
                    store.mark_delivered(mapped["event_id"], "blocked", "split-not-accepted")
                    continue
                message_id = discord.post_and_verify(channel, mapped["content"])
                store.mark_delivered(mapped["event_id"], channel, message_id)
                result[kind] += 1
            except Exception as error:
                store.mark_failed(mapped["event_id"], error)
                raise
    for row in sessions:
        event_id = stripe_event_id(row)
        if not event_id:
            continue
        existing = store.event_status(event_id)
        if existing and existing["status"] != "failed":
            continue
        mapped = mapped_checkout(row)
        if dry_run:
            if mapped:
                existing = store.event_status(mapped["event_id"])
                if not existing or existing["status"] == "failed":
                    result["stripe"] += 1
            continue
        if not mapped:
            if terminally_ignorable_stripe(row):
                digest = payload_hash({"session_id": event_id, "eligible": False})
                if store.claim_event(event_id, "stripe_ignored", digest):
                    store.mark_delivered(event_id, "ignored", "not-collective-product")
            continue
        if not mapped or not store.claim_event(mapped["event_id"], "stripe", mapped["payload_hash"]):
            continue
        try:
            discord.grant_pro(mapped["discord_id"])
            store.mark_delivered(mapped["event_id"], GUILD_ID, mapped["discord_id"])
            result["stripe"] += 1
        except Exception as error:
            store.mark_failed(mapped["event_id"], error)
            raise
    if not dry_run:
        signed = reconcile_signed(store, discord)
        result["signed_reconciled"] = signed["granted"]
        result["signed_removed"] = signed["removed"]
        result["signed_absent"] = signed["absent"]
    return result


def reconcile_signed(store, discord) -> dict[str, int]:
    result = {"granted": 0, "removed": 0, "absent": 0}
    errors: list[BaseException] = []
    valid = set(store.signed_discord_ids())
    protected = set(store.legacy_signed_ids())
    current = set(discord.signed_member_ids())
    desired = valid | protected
    for discord_id in sorted(desired - current):
        try:
            if discord.grant_signed(discord_id):
                result["granted"] += 1
            else:
                result["absent"] += 1
        except Exception as error:
            errors.append(error)
    for discord_id in sorted(current - valid - protected):
        try:
            if discord.remove_signed(discord_id):
                result["removed"] += 1
            else:
                result["absent"] += 1
        except Exception as error:
            errors.append(error)
    if errors:
        raise RuntimeError(f"Signed reconciliation failed for {len(errors)} member(s)")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    home = profile_home()
    lock_path = home / "collective-automation.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "busy"}))
            return 0
        value = run(initialize=args.initialize, dry_run=args.dry_run)
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
