#!/usr/bin/env python3
"""Generate one grounded weekday AGK News digest and verify Discord delivery."""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://discord.com/api/v10"
NEWS_CHANNEL = "1541438210763395113"
HERMES = "/opt/agk-terminal/hermes-agent/venv/bin/hermes"
URL = re.compile(r"https://[^\s>]+")


def should_publish(date: str, *, weekday: int, state_path: str | Path) -> bool:
    if weekday >= 5:
        return False
    path = Path(state_path)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    return state.get("date") != date


def record_published(date: str, message_id: str, state_path: str | Path) -> None:
    path = Path(state_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    temporary.write_text(json.dumps({"date": date, "message_id": message_id}, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def _load_token(home: Path) -> str:
    for raw in (home / ".env").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() in {"DISCORD_BOT_TOKEN", "DISCORD_TOKEN"}:
            return value.strip().strip('"').strip("'")
    raise RuntimeError("Collective Discord credential unavailable")


def _discord(method: str, path: str, token: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": "Bot " + token, "User-Agent": "DiscordBot (https://github.com/agentik-os, 1.0)"}
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


def generate_digest(today: dt.date, home: Path) -> str:
    prompt = f"""Create today's AGK News digest for {today.isoformat()} (Europe/Madrid).
Use web_search/web_extract and only verifiable first-party release/news pages, official repositories, and primary papers published in the last 72 hours. Select up to 7 material items for agentic AI builders: models, tools, methods, systems. Never pad the digest: if no item passes verification, output only the exact heading followed by `Gaps: No verified material items.` Do not invent or repeat stale items. Each selected item must contain: numbered title, category model/tool/method/system, confidence A/B, one direct source URL on its own line, and a compact factual summary with evidence or benchmark caveats. End with a one-line Gaps section for sources that failed. English only. Start exactly `AGK News — {today.strftime('%A %Y-%m-%d')} (Europe/Madrid)`. Plain text, no tables, no preamble, no conclusion, maximum 1900 characters including URLs."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=home, prefix="news-prompt-", suffix=".txt") as handle:
        handle.write(prompt)
        prompt_path = Path(handle.name)
    try:
        result = subprocess.run(
            [HERMES, "chat", "--query-file", str(prompt_path), "--oneshot", "-Q", "-t", "core,web", "--provider", "openai-codex", "-m", "gpt-5.6-terra", "--reasoning", "high", "--max-turns", "30", "--run-budget", "600", "--source", "cron"],
            text=True,
            capture_output=True,
            check=False,
            timeout=660,
            env={**os.environ, "HOME": "/home/mission", "HERMES_HOME": str(home)},
        )
    finally:
        prompt_path.unlink(missing_ok=True)
    if result.returncode:
        raise RuntimeError(f"Hermes news generation failed with exit code {result.returncode}")
    text = result.stdout.strip()
    session_marker = "\nSession: "
    if session_marker in text:
        text = text.split(session_marker, 1)[0].strip()
    heading_at = text.find("AGK News —")
    if heading_at >= 0:
        text = text[heading_at:].strip()
    if not text.startswith("AGK News —"):
        raise RuntimeError("News output has invalid heading")
    if len(text) > 1950:
        raise RuntimeError("News output exceeds Discord safety limit")
    source_count = len(URL.findall(text))
    if source_count == 0:
        if "No verified" in text or "no verified" in text:
            return ""
        raise RuntimeError("News output lacks direct source URLs")
    return text


def publish_and_verify(content: str, token: str) -> str:
    created = _discord("POST", f"/channels/{NEWS_CHANNEL}/messages", token, {"content": content, "allowed_mentions": {"parse": []}})
    message_id = str(created.get("id") or "")
    if not message_id:
        raise RuntimeError("News post returned no message ID")
    reread = _discord("GET", f"/channels/{NEWS_CHANNEL}/messages/{message_id}", token)
    if reread.get("content") != content or str(reread.get("channel_id")) != NEWS_CHANNEL:
        raise RuntimeError("News Discord readback mismatch")
    return message_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    home = Path(os.environ.get("HERMES_HOME") or "/home/mission/.hermes/profiles/collective")
    state_path = home / "collective-news-state.json"
    lock_path = home / "collective-news.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    now = dt.datetime.now(ZoneInfo("Europe/Madrid"))
    with lock_path.open("r+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "busy"}))
            return 0
        if not args.force and not should_publish(now.date().isoformat(), weekday=now.weekday(), state_path=state_path):
            print(json.dumps({"status": "no_change", "date": now.date().isoformat()}))
            return 0
        content = generate_digest(now.date(), home)
        if not content:
            if args.dry_run:
                print(json.dumps({"status": "dry_run", "date": now.date().isoformat(), "reason": "no_verified_items"}))
                return 0
            record_published(now.date().isoformat(), "no-change", state_path)
            print(json.dumps({"status": "no_change", "date": now.date().isoformat(), "reason": "no_verified_items"}))
            return 0
        if args.dry_run:
            print(json.dumps({"status": "dry_run", "characters": len(content), "sources": len(URL.findall(content))}))
            return 0
        message_id = publish_and_verify(content, _load_token(home))
        record_published(now.date().isoformat(), message_id, state_path)
    print(json.dumps({"status": "published", "channel_id": NEWS_CHANNEL, "message_id": message_id, "characters": len(content), "sources": len(URL.findall(content))}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
