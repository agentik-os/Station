import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "overlay/scripts/github_stars_forum_watcher.py"


def _module():
    spec = importlib.util.spec_from_file_location("github_stars_forum_watcher", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _star(index: int):
    return {
        "starred_at": f"2026-08-{(index % 28) + 1:02d}T00:00:00Z",
        "repo": {
            "full_name": f"owner/repo-{index}",
            "name": f"repo-{index}",
            "html_url": f"https://github.com/owner/repo-{index}",
            "description": f"Repository {index}",
            "language": "Python",
            "stargazers_count": index,
            "license": {"spdx_id": "MIT"},
        },
    }


def test_live_sized_difference_is_exactly_seven():
    mod = _module()
    stars = [_star(index) for index in range(132)]
    posted = {f"owner/repo-{index}" for index in range(125)}

    missing = mod.missing_stars(stars, posted)

    assert [item["repo"]["full_name"] for item in missing] == [
        f"owner/repo-{index}" for index in range(125, 132)
    ]


def test_post_matches_collective_format_and_mentions_only_agk_role():
    mod = _module()

    post = mod.render_forum_post(_star(7), role_id="1541850167869702215")

    assert post["name"] == "repo-7"
    assert post["message"]["content"].startswith("<@&1541850167869702215>\n\n**repo-7**")
    assert "Language: Python" in post["message"]["content"]
    assert "Stars: 7" in post["message"]["content"]
    assert "License: MIT" in post["message"]["content"]
    assert "https://github.com/owner/repo-7" in post["message"]["content"]
    assert post["message"]["allowed_mentions"] == {
        "parse": [],
        "roles": ["1541850167869702215"],
    }


class FakeState:
    def __init__(self, posted=(), initialized=True):
        self.value = {"initialized": initialized, "posted": sorted(posted)}
        self.saved = []

    def load(self):
        return {"initialized": self.value["initialized"], "posted": list(self.value["posted"])}

    def save(self, value):
        self.value = {"initialized": value["initialized"], "posted": list(value["posted"])}
        self.saved.append(self.load())


class FakeApi:
    def __init__(self, stars, forum_repos=(), verify=True):
        self.stars = stars
        self.forum_repos = set(forum_repos)
        self.verify = verify
        self.created = []

    def list_stars(self):
        return self.stars

    def scan_forum_repositories(self):
        return set(self.forum_repos)

    def repository_is_in_forum(self, full_name):
        return full_name in self.forum_repos

    def create_forum_post(self, payload):
        self.created.append(payload)
        return f"thread-{len(self.created)}"

    def verify_forum_post(self, thread_id, full_name, role_id):
        return self.verify


def test_watcher_scans_history_once_and_creates_only_missing():
    mod = _module()
    stars = [_star(index) for index in range(3)]
    state = FakeState(initialized=False)
    api = FakeApi(stars, forum_repos={"owner/repo-0", "owner/repo-1"})

    result = mod.run_watcher(api, state, role_id="1541850167869702215")

    assert result == {"stars": 3, "already_posted": 2, "created": 1}
    assert len(api.created) == 1
    assert "owner/repo-2" in state.value["posted"]
    assert state.value["initialized"] is True

    second = mod.run_watcher(api, state, role_id="1541850167869702215")
    assert second == {"stars": 3, "already_posted": 3, "created": 0}
    assert len(api.created) == 1


def test_watcher_does_not_commit_ledger_when_discord_readback_fails():
    mod = _module()
    state = FakeState(posted=(), initialized=True)
    api = FakeApi([_star(9)], verify=False)

    with pytest.raises(RuntimeError, match="readback"):
        mod.run_watcher(api, state, role_id="1541850167869702215")

    assert "owner/repo-9" not in state.value["posted"]


def test_station_packages_five_minute_collective_timer():
    service = (ROOT / "overlay/systemd/agk-github-stars-forum.service").read_text()
    timer = (ROOT / "overlay/systemd/agk-github-stars-forum.timer").read_text()
    installer = (
        (ROOT / "overlay/install.sh").read_text()
        + (ROOT / "overlay/scripts/install-shared-hermes.sh").read_text()
    )

    assert "github_stars_forum_watcher.py" in service
    assert "/home/mission/.hermes/profiles/collective" in service
    assert "OnUnitActiveSec=5min" in timer
    assert "Persistent=true" in timer
    assert "agk-github-stars-forum.timer" in installer
    assert "loginctl enable-linger mission" in installer
    assert 'systemctl start "user@$mission_uid.service"' in installer
    assert '[ -S "/run/user/$mission_uid/bus" ] || {' in installer
