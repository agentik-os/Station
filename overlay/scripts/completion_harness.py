#!/usr/bin/env python3
"""Persistent AGK completion harness and requirement/evidence graph."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REQUIREMENT_STATES = {
    "PENDING", "ACTIVE", "DONE", "VERIFIED", "BLOCKED", "HUMAN_REQUIRED",
    "DEFERRED_BY_HUMAN", "NOT_APPLICABLE",
}
RESOLVED_STATES = {"VERIFIED", "DEFERRED_BY_HUMAN", "NOT_APPLICABLE"}
PROMISE_PATTERN = re.compile(
    r"(?im)(?:^|[.!?]\s+)((?:I will also|I will|Next I will|This includes|The remaining step is)\s+[^.!?\n]+)"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _authority_uid() -> int:
    return 0


def extract_promises(text: str) -> list[str]:
    return [match.group(1).strip() for match in PROMISE_PATTERN.finditer(text or "")]


class CompletionStore:
    @staticmethod
    def _infer_profile(root: Path) -> str:
        value = str(root.resolve())
        for profile, marker in (
            ("collective", "/home/mission/.hermes/profiles/collective/"),
            ("nutrition-os", "/home/operator/.hermes/profiles/nutrition-os/"),
            ("operator", "/home/operator/.hermes/"),
            ("agentik", "/home/agentik/.hermes/"),
            ("mission", "/home/mission/.hermes/"),
            ("private", "/home/private/.hermes/"),
        ):
            if value.startswith(marker): return profile
        return "test"

    def __init__(self, root: Path | str, *, approval_root: Path | str | None = None,
                 oracle_root: Path | str | None = None, profile: str | None = None):
        self.root = Path(root)
        self.profile = profile or self._infer_profile(self.root)
        self.approval_root = Path(approval_root or "/var/lib/station/recovery/approvals")
        self.oracle_root = Path(oracle_root or "/var/lib/station/recovery/oracle")
        self.prompts_root = self.root / "prompts"
        self.packages_root = self.root / "relaunch"
        self.root.mkdir(parents=True, exist_ok=True)
        self.prompts_root.mkdir(parents=True, exist_ok=True)
        self.packages_root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "completion.db"
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._schema()

    def _schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS prompts(
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, source TEXT NOT NULL,
              session_id TEXT NOT NULL, profile TEXT NOT NULL, content_path TEXT NOT NULL,
              sha256 TEXT NOT NULL, source_key TEXT UNIQUE, metadata_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS missions(
              id TEXT PRIMARY KEY, client TEXT, project TEXT, state TEXT NOT NULL DEFAULT 'ACTIVE',
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mission_prompts(
              mission_id TEXT NOT NULL REFERENCES missions(id), prompt_id TEXT NOT NULL REFERENCES prompts(id),
              PRIMARY KEY(mission_id,prompt_id)
            );
            CREATE TABLE IF NOT EXISTS requirements(
              id TEXT PRIMARY KEY, prompt_id TEXT NOT NULL REFERENCES prompts(id),
              mission_id TEXT REFERENCES missions(id), text TEXT NOT NULL, type TEXT NOT NULL DEFAULT 'EXPLICIT',
              status TEXT NOT NULL DEFAULT 'PENDING', provenance TEXT NOT NULL DEFAULT '',
              acceptance_criteria TEXT NOT NULL DEFAULT '', dependencies_json TEXT NOT NULL DEFAULT '[]',
              human_gate INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts(
              id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
              requirement_id TEXT REFERENCES requirements(id), type TEXT NOT NULL, location TEXT NOT NULL,
              creator_run TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evidence(
              id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
              requirement_id TEXT REFERENCES requirements(id), verifier TEXT NOT NULL,
              result TEXT NOT NULL, reference TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS authorizations(
              id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
              requirement_id TEXT REFERENCES requirements(id), actor TEXT NOT NULL, source TEXT NOT NULL,
              scope TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS findings(
              id TEXT PRIMARY KEY, mission_id TEXT NOT NULL REFERENCES missions(id),
              classification TEXT NOT NULL, severity TEXT NOT NULL,
              requirement_ids_json TEXT NOT NULL, human_decision TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, object_id TEXT NOT NULL,
              payload_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
            );
            """
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def event(self, event: str, object_id: str, payload: dict[str, Any] | None = None) -> None:
        self.db.execute(
            "INSERT INTO events(event,object_id,payload_json,created_at) VALUES(?,?,?,?)",
            (event, object_id, json.dumps(payload or {}, sort_keys=True), utc_now()),
        )
        self.db.commit()

    def archive_prompt(self, content: str, *, source: str, session_id: str, profile: str,
                       source_key: str | None = None, metadata: dict[str, Any] | None = None) -> str:
        if not content or not content.strip():
            raise ValueError("prompt content is required")
        digest = sha256_text(content)
        source_key = source_key or f"{profile}:{source}:{session_id}:{digest}"
        existing = self.db.execute("SELECT id,sha256 FROM prompts WHERE source_key=?", (source_key,)).fetchone()
        if existing:
            if str(existing["sha256"]) != digest:
                raise RuntimeError("prompt source identity collision with different content")
            return str(existing["id"])
        prompt_id = new_id("P")
        now = datetime.now(timezone.utc)
        directory = self.prompts_root / now.strftime("%Y/%m/%d")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{prompt_id}.md"
        path.write_bytes(content.encode("utf-8"))
        path.chmod(0o600)
        self.db.execute(
            "INSERT INTO prompts VALUES(?,?,?,?,?,?,?,?,?)",
            (prompt_id, now.isoformat(timespec="seconds"), source, session_id, profile,
             str(path), digest, source_key, json.dumps(metadata or {}, sort_keys=True)),
        )
        self.db.commit()
        self.event("prompt.archived", prompt_id)
        return prompt_id

    def get_prompt(self, prompt_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM prompts WHERE id=?", (prompt_id,)).fetchone()
        if not row:
            raise KeyError(prompt_id)
        return dict(row)

    def prompt_content(self, prompt_id: str) -> str:
        row = self.get_prompt(prompt_id)
        content = Path(row["content_path"]).read_bytes().decode("utf-8")
        if sha256_text(content) != row["sha256"]:
            raise RuntimeError(f"prompt archive integrity failure: {prompt_id}")
        return content

    def create_mission(self, mission_id: str | None, prompt_ids: Iterable[str], *,
                       client: str = "", project: str = "") -> str:
        mission_id = mission_id or new_id("MISS")
        now = utc_now()
        self.db.execute(
            "INSERT INTO missions(id,client,project,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (mission_id, client, project, "ACTIVE", now, now),
        )
        for prompt_id in prompt_ids:
            self.get_prompt(prompt_id)
            self.db.execute("INSERT INTO mission_prompts VALUES(?,?)", (mission_id, prompt_id))
        self.db.commit()
        self.event("mission.created", mission_id)
        return mission_id

    def add_requirement(self, prompt_id: str, text: str, *, mission_id: str | None = None,
                        provenance: str = "", type: str = "EXPLICIT",
                        acceptance_criteria: str = "", dependencies: list[str] | None = None,
                        human_gate: bool = False) -> str:
        self.get_prompt(prompt_id)
        if not text.strip():
            raise ValueError("requirement text is required")
        requirement_id = new_id("REQ")
        now = utc_now()
        status = "HUMAN_REQUIRED" if human_gate else "PENDING"
        self.db.execute(
            "INSERT INTO requirements VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (requirement_id, prompt_id, mission_id, text.strip(), type, status, provenance,
             acceptance_criteria, json.dumps(dependencies or []), int(human_gate), now, now),
        )
        self.db.commit()
        self.event("requirement.created", requirement_id, {"mission_id": mission_id})
        return requirement_id

    def get_requirement(self, requirement_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM requirements WHERE id=?", (requirement_id,)).fetchone()
        if not row:
            raise KeyError(requirement_id)
        return dict(row)

    def set_requirement_status(self, requirement_id: str, status: str) -> None:
        if status not in REQUIREMENT_STATES:
            raise ValueError(f"invalid requirement status: {status}")
        self.db.execute(
            "UPDATE requirements SET status=?,updated_at=? WHERE id=?",
            (status, utc_now(), requirement_id),
        )
        if self.db.total_changes == 0:
            raise KeyError(requirement_id)
        self.db.commit()
        self.event("requirement.status_changed", requirement_id, {"status": status})

    def mark_mission_state(self, mission_id: str, state: str) -> None:
        self.db.execute("UPDATE missions SET state=?,updated_at=? WHERE id=?", (state, utc_now(), mission_id))
        self.db.commit()
        self.event("mission.state_changed", mission_id, {"state": state})

    def add_artifact(self, mission_id: str, requirement_id: str | None, type: str,
                     location: str, creator_run: str = "") -> str:
        artifact_id = new_id("ART")
        self.db.execute(
            "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
            (artifact_id, mission_id, requirement_id, type, location, creator_run, utc_now()),
        )
        self.db.commit()
        self.event("artifact.created", artifact_id, {"mission_id": mission_id})
        return artifact_id

    def add_evidence(self, mission_id: str, requirement_id: str | None, verifier: str,
                     result: str, reference: str) -> str:
        if not verifier.strip() or not reference.strip():
            raise ValueError("evidence verifier and reference are required")
        if result.upper() not in {"PASS", "FAIL"}:
            raise ValueError("evidence result must be PASS or FAIL")
        evidence_id = new_id("EVD")
        self.db.execute(
            "INSERT INTO evidence VALUES(?,?,?,?,?,?,?)",
            (evidence_id, mission_id, requirement_id, verifier, result.upper(), reference, utc_now()),
        )
        self.db.commit()
        self.event("verification.passed" if result.upper() == "PASS" else "verification.failed",
                   evidence_id, {"mission_id": mission_id})
        return evidence_id

    @staticmethod
    def _requirement_digest(row: dict[str, Any]) -> str:
        fields = {key: row.get(key) for key in (
            "id", "prompt_id", "mission_id", "text", "type", "provenance",
            "acceptance_criteria", "dependencies_json", "human_gate",
        )}
        return sha256_text(json.dumps(fields, sort_keys=True, separators=(",", ":")))

    def ledger_digest(self, mission_id: str) -> str:
        prompts = []
        for row in self.db.execute(
            "SELECT p.id,p.sha256 FROM prompts p JOIN mission_prompts mp ON mp.prompt_id=p.id WHERE mp.mission_id=? ORDER BY p.id",
            (mission_id,),
        ):
            prompt = dict(row)
            self.prompt_content(str(prompt["id"]))
            prompts.append(prompt)
        requirements = [dict(row) for row in self.db.execute(
            "SELECT * FROM requirements WHERE mission_id=? ORDER BY id", (mission_id,),
        )]
        artifacts = [dict(row) for row in self.db.execute(
            "SELECT id,requirement_id,type,location,creator_run FROM artifacts WHERE mission_id=? ORDER BY id", (mission_id,),
        )]
        evidence = [dict(row) for row in self.db.execute(
            "SELECT id,requirement_id,verifier,result,reference FROM evidence WHERE mission_id=? AND verifier<>'completion-oracle' ORDER BY id",
            (mission_id,),
        )]
        payload = {"mission_id": mission_id, "prompts": prompts,
                   "requirements": requirements, "artifacts": artifacts, "evidence": evidence}
        return sha256_text(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def _approval_path(self, mission_id: str, requirement_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", mission_id) or not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", requirement_id):
            raise ValueError("invalid approval identity")
        return self.approval_root / self.profile / mission_id / f"{requirement_id}.json"

    def _trusted_authorization(self, mission_id: str, requirement_id: str) -> dict[str, Any] | None:
        path = self._approval_path(mission_id, requirement_id)
        try:
            stat = path.stat()
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if stat.st_uid != _authority_uid() or stat.st_mode & 0o022:
            return None
        if (payload.get("mission_id") != mission_id or payload.get("requirement_id") != requirement_id
                or payload.get("decision") != "APPROVE" or not payload.get("actor")):
            return None
        current = self.db.execute("SELECT * FROM requirements WHERE id=? AND mission_id=?", (requirement_id, mission_id)).fetchone()
        if not current or payload.get("requirement_sha256") != self._requirement_digest(dict(current)):
            return None
        return payload
    def record_authorization(self, mission_id: str, requirement_id: str | None, *,
                             actor: str, source: str, scope: str) -> str:
        if os.geteuid() != 0 or not requirement_id:
            raise PermissionError("human approvals require the root approval gate and exact requirement")
        if not actor.strip() or source not in {"discord", "local-owner"}:
            raise PermissionError("explicit human actor and trusted source are required")
        authorization_id = new_id("AUTH")
        created_at = utc_now()
        requirement = self.get_requirement(requirement_id)
        if requirement.get("mission_id") != mission_id or not requirement.get("human_gate"):
            raise ValueError("authorization target is not this mission's human gate")
        payload = {"id": authorization_id, "mission_id": mission_id, "requirement_id": requirement_id,
                   "requirement_sha256": self._requirement_digest(requirement),
                   "actor": actor.strip(), "source": source, "scope": scope,
                   "decision": "APPROVE", "created_at": created_at, "authority": "root-approval-gate"}
        path = self._approval_path(mission_id, requirement_id)
        gid = self.root.stat().st_gid
        profile_directory = self.approval_root / self.profile
        profile_directory.mkdir(parents=True, exist_ok=True)
        os.chown(profile_directory, _authority_uid(), gid); profile_directory.chmod(0o710)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chown(path.parent, _authority_uid(), gid); path.parent.chmod(0o750)
        temporary = path.with_name(f".{path.name}.new")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chown(temporary, _authority_uid(), gid); temporary.chmod(0o440); os.replace(temporary, path)
        self.db.execute(
            "INSERT INTO authorizations VALUES(?,?,?,?,?,?,?)",
            (authorization_id, mission_id, requirement_id, actor.strip(), source, scope, created_at),
        )
        self.db.commit()
        self.event("mission.authorized", mission_id, {"authorization_id": authorization_id})
        return authorization_id

    def _oracle_path(self, mission_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", mission_id):
            raise ValueError("invalid Oracle mission identity")
        return self.oracle_root / self.profile / f"{mission_id}.json"

    def _trusted_oracle(self, mission_id: str, ledger_sha256: str) -> dict[str, Any] | None:
        path = self._oracle_path(mission_id)
        try:
            stat = path.stat(); payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if stat.st_uid != _authority_uid() or stat.st_mode & 0o022:
            return None
        if (payload.get("mission_id") != mission_id or payload.get("classification") != "COMPLETE"
                or payload.get("gauntlet") != "PASS" or payload.get("requirements_verified") is not True
                or payload.get("ledger_sha256") != ledger_sha256):
            return None
        return payload

    def record_oracle_verdict(self, mission_id: str, *, actor: str, report_sha256: str,
                              ledger_sha256: str) -> str:
        if (os.geteuid() != 0 or not actor.strip()
                or not re.fullmatch(r"[0-9a-f]{64}", report_sha256)
                or not re.fullmatch(r"[0-9a-f]{64}", ledger_sha256)):
            raise PermissionError("trusted root Oracle verdict and SHA-256 values are required")
        if ledger_sha256 != self.ledger_digest(mission_id):
            raise ValueError("Oracle report targets a stale or different requirement ledger")
        gid = self.root.stat().st_gid
        profile_directory = self.oracle_root / self.profile
        profile_directory.mkdir(parents=True, exist_ok=True)
        os.chown(profile_directory, _authority_uid(), gid); profile_directory.chmod(0o710)
        path = self._oracle_path(mission_id)
        payload = {"mission_id": mission_id, "classification": "COMPLETE", "gauntlet": "PASS",
                   "requirements_verified": True, "actor": actor, "report_sha256": report_sha256,
                   "ledger_sha256": ledger_sha256,
                   "authority": "root-oracle-gate", "created_at": utc_now()}
        temporary = path.with_name(f".{path.name}.new")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chown(temporary, _authority_uid(), gid); temporary.chmod(0o440); os.replace(temporary, path)
        return self.add_evidence(mission_id, None, "completion-oracle", "PASS", f"sha256:{report_sha256}")

    def completion_gate(self, mission_id: str) -> dict[str, Any]:
        mission = self.db.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        if not mission:
            raise KeyError(mission_id)
        rows = [dict(row) for row in self.db.execute(
            "SELECT * FROM requirements WHERE mission_id=? ORDER BY created_at,id", (mission_id,)
        )]
        unresolved = [row for row in rows if row["status"] not in RESOLVED_STATES]
        human_required = []
        missing_evidence = []
        for row in rows:
            auth = self._trusted_authorization(mission_id, row["id"])
            if row["human_gate"] and not auth:
                human_required.append(row)
            evidence = self.db.execute(
                "SELECT 1 FROM evidence WHERE mission_id=? AND requirement_id=? AND result='PASS' LIMIT 1",
                (mission_id, row["id"]),
            ).fetchone()
            if row["status"] == "VERIFIED" and not evidence and not (row["human_gate"] and auth):
                missing_evidence.append(row)
        try:
            ledger_sha256 = self.ledger_digest(mission_id)
            integrity_error = ""
        except (OSError, UnicodeError, RuntimeError):
            ledger_sha256 = ""
            integrity_error = "prompt_archive_integrity_failure"
        oracle = self._trusted_oracle(mission_id, ledger_sha256) if ledger_sha256 else None
        permit = (not unresolved and not human_required and not missing_evidence and bool(rows)
                  and bool(oracle) and not integrity_error)
        if str(mission["state"]).upper() == "DONE" and not permit:
            classification = "FALSELY_MARKED_DONE"
        elif permit:
            classification = "COMPLETE"
        elif any(row["status"] == "BLOCKED" for row in rows):
            classification = "BLOCKED"
        else:
            classification = "INCOMPLETE"
        return {
            "mission_id": mission_id, "classification": classification, "permit_done": permit,
            "requirements": rows, "unresolved": unresolved, "human_required": human_required,
            "missing_evidence": missing_evidence, "completion_oracle_passed": bool(oracle),
            "ledger_sha256": ledger_sha256, "integrity_error": integrity_error,
        }

    def create_finding(self, mission_id: str, classification: str, severity: str,
                       requirement_ids: list[str]) -> str:
        finding_id = new_id("FIND")
        now = utc_now()
        self.db.execute(
            "INSERT INTO findings VALUES(?,?,?,?,?,?,?,?)",
            (finding_id, mission_id, classification, severity, json.dumps(requirement_ids), None, now, now),
        )
        self.db.commit()
        self.event("recovery.finding.created", finding_id)
        return finding_id

    def relaunch_finding(self, finding_id: str, *, authorization: dict[str, str]) -> dict[str, Any]:
        if os.geteuid() != 0:
            raise PermissionError("relaunch packages can only be created by the root recovery router")
        required = {"id", "actor", "source", "scope", "timestamp", "authority"}
        if not isinstance(authorization, dict) or not required.issubset(authorization):
            raise PermissionError("root recovery-router authorization attestation is required")
        if (authorization.get("scope") != f"relaunch:{finding_id}"
                or authorization.get("authority") != "root-recovery-router"
                or not authorization.get("actor")):
            raise PermissionError("authorization scope does not match finding")
        finding = self.db.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
        if not finding:
            raise KeyError(finding_id)
        mission_id = str(finding["mission_id"])
        requirement_ids = json.loads(finding["requirement_ids_json"])
        requirements = [self.get_requirement(req_id) for req_id in requirement_ids]
        prompt_ids = sorted({row["prompt_id"] for row in requirements})
        if not prompt_ids:
            prompt_ids = [str(row["prompt_id"]) for row in self.db.execute(
                "SELECT prompt_id FROM mission_prompts WHERE mission_id=? ORDER BY rowid", (mission_id,)
            )]
        prompts = []
        for prompt_id in prompt_ids:
            row = self.get_prompt(prompt_id)
            content = self.prompt_content(prompt_id)
            prompts.append({**row, "content": content})
        artifacts = [dict(row) for row in self.db.execute("SELECT * FROM artifacts WHERE mission_id=?", (mission_id,))]
        evidence = [dict(row) for row in self.db.execute("SELECT * FROM evidence WHERE mission_id=?", (mission_id,))]
        package = {
            "schema": "agk.recovery.v1", "finding_id": finding_id, "mission_id": mission_id,
            "authorization": dict(authorization), "ledger_sha256": self.ledger_digest(mission_id),
            "original_prompts": prompts, "requirements": requirements,
            "artifacts": artifacts, "evidence": evidence,
            "instruction": "Execute only unresolved/reopened requirement nodes; verify and return evidence.",
        }
        path = self.packages_root / f"{finding_id}.json"
        path.write_text(json.dumps(package, indent=2, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)
        self.db.execute("UPDATE findings SET human_decision='RELAUNCH',updated_at=? WHERE id=?", (utc_now(), finding_id))
        self.db.commit()
        self.event("mission.relaunched", mission_id, {"finding_id": finding_id, "package": str(path)})
        return package


def _default_root() -> Path:
    return Path.home() / ".hermes" / "completion"


def main() -> int:
    parser = argparse.ArgumentParser(description="AGK completion harness")
    parser.add_argument("--root", type=Path, default=_default_root())
    sub = parser.add_subparsers(dest="action", required=True)
    archive = sub.add_parser("archive-prompt")
    archive.add_argument("--source", required=True); archive.add_argument("--session", required=True)
    archive.add_argument("--profile", required=True); archive.add_argument("--file", type=Path)
    gate = sub.add_parser("gate"); gate.add_argument("mission")
    args = parser.parse_args()
    store = CompletionStore(args.root)
    if args.action == "archive-prompt":
        content = args.file.read_text(encoding="utf-8") if args.file else sys.stdin.read()
        print(store.archive_prompt(content, source=args.source, session_id=args.session, profile=args.profile))
    elif args.action == "gate":
        print(json.dumps(store.completion_gate(args.mission), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
