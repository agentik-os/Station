#!/usr/bin/env python3
"""Transactionally transfer the Collective Discord gateway from Mission to Agentik."""
from __future__ import annotations

import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_ID = "1541131574509314209"
CONTROL_GUILD_ID = "1541131439599386644"
HOME_CHANNEL_ID = "1541847685680603387"
COMMUNITY_GUILD_ID = "1350170767366688830"
FORUM_CHANNEL_ID = "1541222874226888804"
OLD_USER = "mission"
NEW_USER = "agentik"
OLD_ROOT = Path("/home/mission/.hermes")
NEW_ROOT = Path("/home/agentik/.hermes")
PROFILE = "collective"
UNIT = "hermes-gateway-collective.service"
OLD_FRAGMENT = "/home/mission/.config/systemd/user/hermes-gateway-collective.service"
NEW_FRAGMENT = "/home/agentik/.config/systemd/user/hermes-gateway-collective.service"
TOKEN_KEY = "DISCORD_BOT_TOKEN"
TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{40,}$")
OLD_TIMERS = ("agk-github-stars-forum.timer", "agk-collective-composio.timer", "agk-collective-news.timer")
CANONICAL_ROUTING = {
    "DISCORD_ALLOWED_USERS": "1441423462492016821,1541816910587625492,1541817649661747351,1541817976586637382,1541817162241540126,1541131574509314209",
    "DISCORD_ALLOW_ALL_USERS": "false",
    "DISCORD_ALLOW_BOTS": "mentions",
    "DISCORD_AUTO_THREAD": "false",
    "DISCORD_HOME_CHANNEL": "1541847685680603387",
    "DISCORD_HOME_CHANNEL_THREAD_ID": "",
    "DISCORD_REQUIRE_MENTION": "false",
    "DISCORD_MESSAGE_CONTENT_INTENT": "true",
    "DISCORD_BOTS_REQUIRE_INLINE_MENTION": "true",
}
CLIENT_CREDENTIAL_PREFIXES = ("STRIPE_", "TYPEFORM_", "COMPOSIO_", "DENTISTRY_")
CREDENTIAL_MARKERS = ("TOKEN", "SECRET", "PASSWORD", "PRIVATE_KEY", "API_KEY", "WEBHOOK_KEY")


def _assignment_key(line: str) -> str:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return ""
    return stripped.split("=", 1)[0].strip()


def _is_discord_credential_key(key: str) -> bool:
    return key == TOKEN_KEY or (key.startswith("DISCORD_") and "TOKEN" in key)


def validate_source_without_discord(content: str) -> None:
    for line in content.splitlines():
        key = _assignment_key(line)
        if _is_discord_credential_key(key):
            raise RuntimeError("source still contains a Discord credential")


def extract_token(content: str) -> tuple[str, str]:
    token = ""
    token_seen = False
    kept = []
    for line in content.splitlines():
        key = _assignment_key(line)
        if key == TOKEN_KEY:
            if token_seen:
                raise RuntimeError("duplicate Collective Discord token")
            token_seen = True
            token = line.split("=", 1)[1].strip().strip('"').strip("'")
        elif _is_discord_credential_key(key):
            raise RuntimeError("source contains an alternate Discord credential")
        else:
            kept.append(line)
    if not TOKEN_RE.fullmatch(token):
        raise RuntimeError("source Collective Discord token is missing or invalid")
    result = "\n".join(kept).rstrip() + "\n"
    validate_source_without_discord(result)
    return token, result


def _env_rows(content: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in seen:
            raise RuntimeError("target environment contains an invalid or duplicate key")
        seen.add(key)
        rows.append((key, value.strip().strip('"').strip("'")))
    return rows


def _reject_target_credentials(rows: list[tuple[str, str]]) -> None:
    for key, _ in rows:
        if key in {TOKEN_KEY, "DISCORD_TOKEN"}:
            raise RuntimeError("target already contains a Discord credential")
        if key.startswith(CLIENT_CREDENTIAL_PREFIXES) or any(marker in key for marker in CREDENTIAL_MARKERS):
            raise RuntimeError("target contains a client or unrelated credential")


def validate_target_env(content: str, token: str) -> None:
    rows = _env_rows(content)
    values = dict(rows)
    if len(rows) != len(values):
        raise RuntimeError("target environment contains duplicate keys")
    if values.get(TOKEN_KEY) != token or not TOKEN_RE.fullmatch(values.get(TOKEN_KEY, "")):
        raise RuntimeError("target Discord credential readback mismatch")
    for key, expected in CANONICAL_ROUTING.items():
        if values.get(key) != expected:
            raise RuntimeError(f"target Collective routing mismatch: {key}")
    for key, _ in rows:
        if key == TOKEN_KEY or key in CANONICAL_ROUTING:
            continue
        if key == "DISCORD_TOKEN" or key.startswith(CLIENT_CREDENTIAL_PREFIXES) or any(marker in key for marker in CREDENTIAL_MARKERS):
            raise RuntimeError("target contains an unexpected credential after canonicalization")


def target_env_content(content: str, token: str) -> str:
    rows = _env_rows(content)
    _reject_target_credentials(rows)
    replaced = {TOKEN_KEY, *CANONICAL_ROUTING}
    kept = []
    for line in content.splitlines():
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else ""
        if key not in replaced:
            kept.append(line)
    canonical = [f"{TOKEN_KEY}={token}", *[f"{key}={value}" for key, value in CANONICAL_ROUTING.items()]]
    result = "\n".join(kept).rstrip() + "\n" + "\n".join(canonical) + "\n"
    validate_target_env(result, token)
    return result


def atomic_write(path: Path, content: str, uid: int, gid: int) -> None:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name + ".new.", delete=False)
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, uid, gid)
        temporary.chmod(0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def account(user: str): return pwd.getpwnam(user)


def user_env(user: str, root: Path) -> dict[str, str]:
    row = account(user); runtime = f"/run/user/{row.pw_uid}"
    return {"HOME": row.pw_dir, "HERMES_HOME": str(root), "PATH": "/opt/agk-terminal/hermes-agent/venv/bin:/usr/local/bin:/usr/bin:/bin", "XDG_RUNTIME_DIR": runtime, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}


def run_user(user: str, root: Path, argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["sudo", "-u", user, "env", *[f"{k}={v}" for k,v in user_env(user,root).items()], *argv], text=True, capture_output=True, timeout=180, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{user} command failed with exit code {result.returncode}")
    return result


def service_state(user: str, root: Path) -> dict[str, str]:
    result = run_user(user, root, ["systemctl", "--user", "show", UNIT, "-p", "LoadState", "-p", "ActiveState", "-p", "UnitFileState", "-p", "MainPID", "-p", "FragmentPath"])
    return dict(line.split("=",1) for line in result.stdout.splitlines() if "=" in line)


def verify_service_state(
    user: str,
    root: Path,
    *,
    expected_active: str,
    expected_unit_file: str,
) -> dict[str, str]:
    state = service_state(user, root)
    if state.get("ActiveState") != expected_active or state.get("UnitFileState") != expected_unit_file:
        raise RuntimeError(
            f"{user} {UNIT} state mismatch: active={state.get('ActiveState')} unit_file={state.get('UnitFileState')}"
        )
    return state


def restore_env_files(
    old_env: Path,
    new_env: Path,
    old_original: str,
    new_original: str,
    old_uid: int,
    old_gid: int,
    new_uid: int,
    new_gid: int,
) -> None:
    errors: list[str] = []
    # Remove the target credential first; the target gateway must already be
    # proven inactive and disabled by the caller.
    for label, path, content, uid, gid in (
        ("target", new_env, new_original, new_uid, new_gid),
        ("source", old_env, old_original, old_uid, old_gid),
    ):
        try:
            atomic_write(path, content, uid, gid)
        except Exception as error:
            errors.append(f"{label} restore write failed: {type(error).__name__}")
    for label, path, content in (("target", new_env, new_original), ("source", old_env, old_original)):
        try:
            if path.read_text() != content:
                errors.append(f"{label} restore readback mismatch")
        except Exception as error:
            errors.append(f"{label} restore readback failed: {type(error).__name__}")
    if errors:
        raise RuntimeError("credential rollback incomplete: " + "; ".join(errors))


def transfer_env_files(
    old_env: Path,
    new_env: Path,
    old_original: str,
    new_original: str,
    old_without: str,
    new_with: str,
    old_uid: int,
    old_gid: int,
    new_uid: int,
    new_gid: int,
) -> None:
    try:
        atomic_write(new_env, new_with, new_uid, new_gid)
        if new_env.read_text() != new_with:
            raise RuntimeError("target credential write readback mismatch")
        atomic_write(old_env, old_without, old_uid, old_gid)
        if old_env.read_text() != old_without:
            raise RuntimeError("source credential removal readback mismatch")
    except Exception:
        restore_env_files(
            old_env,
            new_env,
            old_original,
            new_original,
            old_uid,
            old_gid,
            new_uid,
            new_gid,
        )
        raise


def gateway_action(user: str, root: Path, action: str) -> None:
    run_user(user, root, ["/opt/agk-terminal/hermes-agent/venv/bin/hermes", "--profile", PROFILE, "gateway", action])


def discord_probe(token: str) -> dict[str, str]:
    headers={"Authorization":"Bot "+token,"User-Agent":"AGK-Collective-Transfer/1.0"}
    def get(path):
        try:
            with urllib.request.urlopen(urllib.request.Request("https://discord.com/api/v10"+path,headers=headers),timeout=30) as response: return json.load(response)
        except (urllib.error.HTTPError,urllib.error.URLError,ValueError,TimeoutError) as error:
            raise RuntimeError("Discord transfer identity probe failed") from error
    identity=get("/users/@me")
    home=get("/channels/"+HOME_CHANNEL_ID)
    forum=get("/channels/"+FORUM_CHANNEL_ID)
    if (str(identity.get("id")) != APP_ID or not identity.get("bot") or
        str(home.get("id")) != HOME_CHANNEL_ID or str(home.get("guild_id")) != CONTROL_GUILD_ID or
        str(forum.get("id")) != FORUM_CHANNEL_ID or str(forum.get("guild_id")) != COMMUNITY_GUILD_ID or
        forum.get("type") != 15):
        raise RuntimeError("Collective Discord identity/control/community route mismatch")
    return {
        "id": APP_ID,
        "control_guild_id": CONTROL_GUILD_ID,
        "home_channel_id": HOME_CHANNEL_ID,
        "community_guild_id": COMMUNITY_GUILD_ID,
        "forum_channel_id": FORUM_CHANNEL_ID,
    }


def active_agents() -> int:
    path=OLD_ROOT/"profiles"/PROFILE/"gateway_state.json"
    try: value=json.loads(path.read_text()).get("active_agents",0)
    except Exception as error: raise RuntimeError("old Collective gateway state is unreadable") from error
    if not isinstance(value,int) or value < 0: raise RuntimeError("old active_agents is invalid")
    return value


def old_timer_states() -> dict[str, dict[str, str]]:
    result = {}
    for unit in OLD_TIMERS:
        state = run_user(OLD_USER, OLD_ROOT, ["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "UnitFileState"], check=False)
        values = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
        if values.get("ActiveState") not in {"active", "inactive"} or values.get("UnitFileState") not in {"enabled", "disabled"}:
            raise RuntimeError(f"unsupported old Mission timer baseline: {unit}")
        result[unit] = values
    return result


def verify_timer_states(states: dict[str, dict[str, str]]) -> None:
    for unit, expected in states.items():
        result = run_user(OLD_USER, OLD_ROOT, ["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "UnitFileState"], check=False)
        actual = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if actual.get("ActiveState") != expected.get("ActiveState") or actual.get("UnitFileState") != expected.get("UnitFileState"):
            raise RuntimeError(f"old Mission timer restore mismatch: {unit}")


def freeze_old_automations() -> None:
    for unit in OLD_TIMERS:
        run_user(OLD_USER, OLD_ROOT, ["systemctl", "--user", "disable", "--now", unit], check=False)
        state = run_user(OLD_USER, OLD_ROOT, ["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "UnitFileState"], check=False)
        values = dict(line.split("=", 1) for line in state.stdout.splitlines() if "=" in line)
        if values.get("ActiveState") not in {"inactive", "failed"} or values.get("UnitFileState") not in {"disabled", "static", "not-found"}:
            raise RuntimeError(f"old Mission timer did not freeze: {unit}")


def restore_old_automations(states: dict[str, dict[str, str]]) -> None:
    for unit, state in states.items():
        enabled = state["UnitFileState"] == "enabled"
        active = state["ActiveState"] == "active"
        run_user(OLD_USER, OLD_ROOT, ["systemctl", "--user", "enable" if enabled else "disable", unit], check=False)
        run_user(OLD_USER, OLD_ROOT, ["systemctl", "--user", "start" if active else "stop", unit], check=False)
    verify_timer_states(states)


def wait_gateway_state(root: Path, expected_pid: int, timeout: int = 90) -> dict:
    path=root/"profiles"/PROFILE/"gateway_state.json"; deadline=time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            value=json.loads(path.read_text()); platform=(value.get("platforms") or {}).get("discord") or {}
            if (value.get("gateway_state")=="running" and platform.get("state")=="connected" and
                value.get("pid")==expected_pid and platform.get("writer_pid")==expected_pid and
                value.get("start_time")==platform.get("writer_start_time") and
                str(value.get("hermes_home"))==str(root/"profiles"/PROFILE)):
                return value
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("new Collective gateway writer state did not converge")


def migrate() -> dict[str, object]:
    if os.geteuid() != 0: raise PermissionError("Collective ownership transfer requires root")
    old_env=OLD_ROOT/"profiles"/PROFILE/".env"; new_env=NEW_ROOT/"profiles"/PROFILE/".env"
    old_account=account(OLD_USER); new_account=account(NEW_USER)
    for path,owner in ((old_env,old_account),(new_env,new_account)):
        if not path.is_file() or path.is_symlink() or path.stat().st_uid != owner.pw_uid: raise RuntimeError("unsafe Collective token store")
    old_state=service_state(OLD_USER,OLD_ROOT); new_state=service_state(NEW_USER,NEW_ROOT)
    if (old_state.get("ActiveState") != "active" or old_state.get("UnitFileState") not in {"enabled", "disabled"} or old_state.get("FragmentPath") != OLD_FRAGMENT or
        new_state.get("LoadState") != "loaded" or new_state.get("ActiveState") != "inactive" or new_state.get("UnitFileState") != "disabled" or
        new_state.get("FragmentPath") != NEW_FRAGMENT): raise RuntimeError("gateway transfer baseline mismatch")
    if active_agents() != 0: raise RuntimeError("old Collective gateway still has active work")
    old_original=old_env.read_text(); new_original=new_env.read_text()
    token,old_without=extract_token(old_original); probe=discord_probe(token)
    new_with=target_env_content(new_original,token)
    timer_baseline=old_timer_states(); old_stopped=False
    try:
        freeze_old_automations()
        old_stopped=True
        gateway_action(OLD_USER,OLD_ROOT,"stop")
        verify_service_state(
            OLD_USER,
            OLD_ROOT,
            expected_active="inactive",
            expected_unit_file=old_state["UnitFileState"],
        )
        transfer_env_files(
            old_env,
            new_env,
            old_original,
            new_original,
            old_without,
            new_with,
            old_account.pw_uid,
            old_account.pw_gid,
            new_account.pw_uid,
            new_account.pw_gid,
        )
        validate_source_without_discord(old_env.read_text())
        validate_target_env(new_env.read_text(), token)
        token=""
        gateway_action(NEW_USER,NEW_ROOT,"start")
        final=verify_service_state(
            NEW_USER,
            NEW_ROOT,
            expected_active="active",
            expected_unit_file="disabled",
        )
        if int(final.get("MainPID") or 0) <= 0: raise RuntimeError("new Collective gateway has no main PID")
        writer=wait_gateway_state(NEW_ROOT,int(final["MainPID"]))
        run_user(NEW_USER,NEW_ROOT,["systemctl","--user","enable",UNIT])
        verify_service_state(
            NEW_USER,
            NEW_ROOT,
            expected_active="active",
            expected_unit_file="enabled",
        )
        run_user(OLD_USER,OLD_ROOT,["systemctl","--user","disable",UNIT],check=False)
        verify_service_state(
            OLD_USER,
            OLD_ROOT,
            expected_active="inactive",
            expected_unit_file="disabled",
        )
        return {"status":"migrated","old_owner":OLD_USER,"new_owner":NEW_USER,"unit":UNIT,"pid":int(final["MainPID"]),"writer_start_time":writer["start_time"],**probe}
    except Exception as original_error:
        rollback_errors: list[str] = []
        # Credentials may only return to Mission after Agentik is proven unable
        # to reconnect with the same token.
        run_user(NEW_USER,NEW_ROOT,["/opt/agk-terminal/hermes-agent/venv/bin/hermes","--profile",PROFILE,"gateway","stop"],check=False)
        run_user(NEW_USER,NEW_ROOT,["systemctl","--user","disable",UNIT],check=False)
        try:
            verify_service_state(
                NEW_USER,
                NEW_ROOT,
                expected_active="inactive",
                expected_unit_file="disabled",
            )
        except Exception as error:
            raise RuntimeError(
                "rollback incomplete: target gateway is not proven inactive and disabled; source credentials were not restored"
            ) from error
        try:
            restore_env_files(
                old_env,
                new_env,
                old_original,
                new_original,
                old_account.pw_uid,
                old_account.pw_gid,
                new_account.pw_uid,
                new_account.pw_gid,
            )
        except Exception as error:
            rollback_errors.append(str(error))
        old_ready = not old_stopped
        if not rollback_errors and old_stopped:
            try:
                if old_state["UnitFileState"] == "enabled":
                    run_user(OLD_USER,OLD_ROOT,["systemctl","--user","enable",UNIT],check=False)
                else:
                    run_user(OLD_USER,OLD_ROOT,["systemctl","--user","disable",UNIT],check=False)
                gateway_action(OLD_USER,OLD_ROOT,"start")
                restored=verify_service_state(
                    OLD_USER,
                    OLD_ROOT,
                    expected_active="active",
                    expected_unit_file=old_state["UnitFileState"],
                )
                if int(restored.get("MainPID") or 0) <= 0: raise RuntimeError("restored old gateway has no main PID")
                wait_gateway_state(OLD_ROOT,int(restored["MainPID"]))
                old_ready = True
            except Exception as error:
                rollback_errors.append(f"old gateway restore failed: {type(error).__name__}")
        if old_ready:
            try:
                restore_old_automations(timer_baseline)
            except Exception as error:
                rollback_errors.append(f"timer restore failed: {type(error).__name__}")
        else:
            rollback_errors.append("timers left frozen because old gateway was not safely restored")
        if rollback_errors:
            raise RuntimeError("rollback incomplete: " + "; ".join(rollback_errors)) from original_error
        raise


def main() -> int:
    print(json.dumps(migrate(),sort_keys=True))
    return 0

if __name__ == "__main__": raise SystemExit(main())
