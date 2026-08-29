#!/usr/bin/env python3
"""Mirror new agentik-os GitHub Stars into the Collective Discord forum."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GITHUB_ACCOUNT = "agentik-os"
FORUM_CHANNEL_ID = "1541222874226888804"
FORUM_GUILD_ID = "1350170767366688830"
AGK_ROLE_ID = "1541850167869702215"
GITHUB_REPO_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)


def normalize_repository(value: str) -> str:
    return str(value or "").strip().removesuffix(".git").lower()


def repository_key(star: dict) -> str:
    return normalize_repository(star["repo"]["full_name"])


def missing_stars(stars: list[dict], posted: set[str]) -> list[dict]:
    normalized = {normalize_repository(item) for item in posted}
    return [star for star in stars if repository_key(star) not in normalized]


def repositories_from_text(text: str) -> set[str]:
    found = set()
    for owner, repo in GITHUB_REPO_RE.findall(str(text or "")):
        cleaned = repo.rstrip(".,);]")
        found.add(normalize_repository(f"{owner}/{cleaned}"))
    return found


def render_forum_post(star: dict, role_id: str = AGK_ROLE_ID) -> dict:
    repo = star["repo"]
    name = str(repo.get("name") or repo["full_name"].split("/", 1)[-1])[:100]
    description = " ".join(str(repo.get("description") or "No description provided.").split())
    description = description[:600]
    language = str(repo.get("language") or "Unknown")
    stars = int(repo.get("stargazers_count") or 0)
    license_value = (repo.get("license") or {}).get("spdx_id") or "Other"
    if license_value in {"NOASSERTION", "NONE"}:
        license_value = "Other"
    url = str(repo["html_url"])
    content = (
        f"<@&{role_id}>\n\n"
        f"**{name}**\n{description}\n\n"
        f"Language: {language}\n"
        f"Stars: {stars}\n"
        f"License: {license_value}\n"
        f"{url}\n\n"
        "Unlock with Pro — COLLECTIVE70"
    )
    return {
        "name": name,
        "message": {
            "content": content[:2000],
            "allowed_mentions": {"parse": [], "roles": [str(role_id)]},
        },
    }


class JsonState:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"initialized": False, "posted": []}
        if not isinstance(value, dict):
            raise RuntimeError("invalid watcher state")
        return {
            "initialized": bool(value.get("initialized")),
            "posted": sorted({normalize_repository(x) for x in value.get("posted", [])}),
        }

    def save(self, value: dict) -> None:
        payload = {
            "initialized": bool(value["initialized"]),
            "posted": sorted({normalize_repository(x) for x in value["posted"]}),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.path)


class Api:
    def __init__(self, discord_token: str):
        self.discord_token = discord_token

    def request_json(
        self,
        method: str,
        url: str,
        *,
        discord: bool = False,
        payload: dict | None = None,
    ):
        headers = {
            "User-Agent": "AGK-GitHub-Stars-Forum/1.0",
            "Accept": "application/json",
        }
        if discord:
            headers["Authorization"] = f"Bot {self.discord_token}"
        elif "api.github.com" in url:
            headers["Accept"] = "application/vnd.github.star+json"
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        for attempt in range(6):
            request = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                raw = error.read().decode("utf-8", errors="replace")
                if error.code == 429:
                    try:
                        delay = float(json.loads(raw).get("retry_after", 1))
                    except (ValueError, TypeError, json.JSONDecodeError):
                        delay = 1.0
                    time.sleep(min(30.0, max(0.1, delay)))
                    continue
                if 500 <= error.code < 600 and attempt < 5:
                    time.sleep(min(16, 2**attempt))
                    continue
                raise RuntimeError(f"HTTP {error.code} from provider") from error
            except (urllib.error.URLError, TimeoutError) as error:
                if attempt == 5:
                    raise RuntimeError("provider request failed") from error
                time.sleep(min(16, 2**attempt))
        raise RuntimeError("provider retries exhausted")

    def list_stars(self) -> list[dict]:
        stars = []
        for page in range(1, 51):
            batch = self.request_json(
                "GET",
                f"https://api.github.com/users/{GITHUB_ACCOUNT}/starred?per_page=100&page={page}",
            )
            stars.extend(batch)
            if len(batch) < 100:
                return stars
        raise RuntimeError("GitHub pagination safety limit reached")

    def _forum_threads(self) -> list[dict]:
        active = self.request_json(
            "GET",
            f"https://discord.com/api/v10/guilds/{FORUM_GUILD_ID}/threads/active",
            discord=True,
        ).get("threads", [])
        threads = {
            item["id"]: item
            for item in active
            if item.get("parent_id") == FORUM_CHANNEL_ID
        }
        before = None
        while True:
            url = (
                f"https://discord.com/api/v10/channels/{FORUM_CHANNEL_ID}"
                "/threads/archived/public?limit=100"
            )
            if before:
                url += "&before=" + urllib.parse.quote(before)
            page = self.request_json("GET", url, discord=True)
            batch = page.get("threads", [])
            for item in batch:
                threads[item["id"]] = item
            if not page.get("has_more") or not batch:
                break
            before = batch[-1].get("thread_metadata", {}).get("archive_timestamp")
            if not before:
                break
        return list(threads.values())

    def _thread_messages(self, thread_id: str) -> list[dict]:
        messages = []
        before = None
        while True:
            url = f"https://discord.com/api/v10/channels/{thread_id}/messages?limit=100"
            if before:
                url += "&before=" + before
            batch = self.request_json("GET", url, discord=True)
            messages.extend(batch)
            if len(batch) < 100:
                break
            before = batch[-1]["id"]
        return messages

    @staticmethod
    def _repositories_in_messages(messages: list[dict]) -> set[str]:
        found = set()
        for message in messages:
            texts = [message.get("content", "")]
            for embed in message.get("embeds", []):
                texts.extend(str(embed.get(key, "")) for key in ("url", "title", "description"))
                texts.extend(str(field.get("value", "")) for field in embed.get("fields", []))
            for text in texts:
                found.update(repositories_from_text(text))
        return found

    def scan_forum_repositories(self) -> set[str]:
        found = set()
        for thread in self._forum_threads():
            found.update(self._repositories_in_messages(self._thread_messages(thread["id"])))
        return found

    def repository_is_in_forum(self, full_name: str) -> bool:
        expected = normalize_repository(full_name)
        repo_name = expected.split("/", 1)[-1]
        for thread in self._forum_threads():
            if str(thread.get("name") or "").lower() != repo_name:
                continue
            if expected in self._repositories_in_messages(self._thread_messages(thread["id"])):
                return True
        return False

    def create_forum_post(self, payload: dict) -> str:
        result = self.request_json(
            "POST",
            f"https://discord.com/api/v10/channels/{FORUM_CHANNEL_ID}/threads",
            discord=True,
            payload=payload,
        )
        return str(result["id"])

    def verify_forum_post(self, thread_id: str, full_name: str, role_id: str) -> bool:
        messages = self._thread_messages(thread_id)
        expected = normalize_repository(full_name)
        role = f"<@&{role_id}>"
        return any(
            expected in self._repositories_in_messages([message])
            and role in str(message.get("content") or "")
            for message in messages
        )


def run_watcher(api, state_store, role_id: str = AGK_ROLE_ID) -> dict:
    stars = api.list_stars()
    state = state_store.load()
    posted = {normalize_repository(item) for item in state.get("posted", [])}
    if not state.get("initialized"):
        posted.update(api.scan_forum_repositories())
        state = {"initialized": True, "posted": sorted(posted)}
        state_store.save(state)

    star_keys = {repository_key(star) for star in stars}
    already_posted = len(star_keys & posted)
    created = 0
    candidates = sorted(missing_stars(stars, posted), key=lambda item: item["starred_at"])
    for star in candidates:
        full_name = repository_key(star)
        if api.repository_is_in_forum(full_name):
            posted.add(full_name)
            state_store.save({"initialized": True, "posted": sorted(posted)})
            continue
        payload = render_forum_post(star, role_id=role_id)
        thread_id = api.create_forum_post(payload)
        if not api.verify_forum_post(thread_id, full_name, role_id):
            raise RuntimeError(f"Discord readback failed for {full_name}")
        posted.add(full_name)
        state_store.save({"initialized": True, "posted": sorted(posted)})
        created += 1
    return {"stars": len(stars), "already_posted": already_posted, "created": created}


def read_discord_token(profile_home: Path) -> str:
    env_path = profile_home / ".env"
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped.startswith("DISCORD_BOT_TOKEN="):
            value = stripped.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if value:
                return value
    raise RuntimeError("DISCORD_BOT_TOKEN is unavailable")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--profile-home",
        type=Path,
        default=Path("/home/agentik/.hermes/profiles/collective"),
    )
    args = parser.parse_args()
    profile_home = args.profile_home.resolve()
    state_path = profile_home / "github-stars-forum-state.json"
    lock_path = profile_home / "github-stars-forum.lock"
    lock_path.touch(mode=0o600, exist_ok=True)
    with lock_path.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_watcher(
            Api(read_discord_token(profile_home)),
            JsonState(state_path),
            role_id=AGK_ROLE_ID,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
