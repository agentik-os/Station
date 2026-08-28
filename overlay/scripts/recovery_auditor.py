#!/usr/bin/env python3
"""Profile-local historical and daily AGK recovery auditor."""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HARNESS = Path(__file__).with_name("completion_harness.py")
_spec = importlib.util.spec_from_file_location("agk_completion_harness", _HARNESS)
if _spec is None or _spec.loader is None:
    raise RuntimeError("completion harness unavailable")
_harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_harness)
CompletionStore = _harness.CompletionStore
extract_promises = _harness.extract_promises
sha256_text = _harness.sha256_text

_ACTIONABLE = re.compile(r"(?i)\b(must|should|need(?:s|ed)?|require(?:s|d)?|create|build|add|fix|verify|ensure|implement|deploy|publish|never|only humans?|do not|don't)\b")
_BULLET = re.compile(r"^\s*(?:[-*+] |\[[ xX]\] |\d+[.)]\s+)(.+?)\s*$")

def extract_candidate_requirements(text: str, limit: int = 200) -> list[str]:
    result=[]; seen=set()
    for raw in str(text or "").splitlines():
        line=raw.strip()
        if not line or line.startswith("```") or line.startswith("#"):
            continue
        match=_BULLET.match(raw)
        candidate=(match.group(1).strip() if match else line)
        if not match and (len(candidate)>500 or not _ACTIONABLE.search(candidate)):
            continue
        candidate=re.sub(r"\s+"," ",candidate).strip()
        key=candidate.casefold()
        if len(candidate)<4 or key in seen:
            continue
        seen.add(key); result.append(candidate)
        if len(result)>=limit: break
    return result


def _readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _session_messages(state_db: Path) -> list[dict[str, Any]]:
    if not state_db.is_file():
        return []
    db = _readonly(state_db)
    try:
        rows = db.execute(
            """
            SELECT m.id,m.session_id,m.role,m.content,m.timestamp,
                   COALESCE(s.source,'unknown') AS source,COALESCE(s.title,'') AS title
            FROM messages m LEFT JOIN sessions s ON s.id=m.session_id
            WHERE COALESCE(m.active,1)=1 AND m.role IN ('user','assistant')
              AND m.content IS NOT NULL AND TRIM(m.content)<>''
            ORDER BY m.timestamp,m.id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        db.close()


def _mission_for_prompt(store: CompletionStore, prompt_id: str, session_id: str) -> str:
    row = store.db.execute(
        "SELECT mission_id FROM mission_prompts WHERE prompt_id=? LIMIT 1", (prompt_id,)
    ).fetchone()
    if row:
        return str(row["mission_id"])
    suffix = sha256_text(session_id)[:12]
    mission_id = f"HIST-{suffix}"
    exists = store.db.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone()
    if not exists:
        store.create_mission(mission_id, [prompt_id], project="historical-recovery")
    else:
        store.db.execute("INSERT OR IGNORE INTO mission_prompts VALUES(?,?)", (mission_id, prompt_id))
        store.db.commit()
    return mission_id


def _report_paths(reports_root: Path, baseline: bool) -> tuple[Path, Path]:
    now = datetime.now(timezone.utc)
    if baseline:
        directory = reports_root / "recovery"
        return directory / "AGK_RECOVERY_BASELINE.md", directory / "AGK_OPERATOR_RECOVERY.md"
    directory = reports_root / "completion" / now.strftime("%Y-%m-%d")
    return directory / "AGK_DAILY_COMPLETION_AUDIT.md", directory / "AGK_OPERATOR_RECOVERY.md"


def _classify(store: CompletionStore) -> list[dict[str, Any]]:
    results = []
    missions = store.db.execute("SELECT id,state,client,project FROM missions ORDER BY created_at").fetchall()
    for mission in missions:
        gate = store.completion_gate(str(mission["id"]))
        classification = gate["classification"]
        if not gate["requirements"]:
            classification = "UNKNOWN / INSUFFICIENT HISTORY"
        elif all(row["type"] == "PROMISE" for row in gate["requirements"]):
            classification = "PROMISED BUT NOT FOUND"
        results.append({**gate, "classification": classification, "state": mission["state"]})
    return results


def _write_reports(store: CompletionStore, profile: str, reports_root: Path,
                   baseline: bool, prompts_reviewed: int) -> tuple[Path, Path, list[dict[str, Any]]]:
    audit_path, operator_path = _report_paths(reports_root, baseline)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    findings = _classify(store)
    for finding in findings:
        store.db.execute(
            "UPDATE findings SET human_decision='SUPERSEDED',updated_at=? WHERE mission_id=? AND classification<>? AND human_decision IS NULL",
            (_harness.utc_now(), finding["mission_id"], finding["classification"]),
        ); store.db.commit()
        if finding["classification"] == "COMPLETE":
            store.db.execute(
                "UPDATE findings SET human_decision='RESOLVED',updated_at=? WHERE mission_id=? AND human_decision IS NULL",
                (_harness.utc_now(), finding["mission_id"]),
            ); store.db.commit(); continue
        requirement_ids = [row["id"] for row in (finding["unresolved"] or finding["requirements"])]
        existing = store.db.execute(
            "SELECT 1 FROM findings WHERE mission_id=? AND classification=? LIMIT 1",
            (finding["mission_id"], finding["classification"]),
        ).fetchone()
        if not existing:
            severity = "P1" if finding["classification"] == "FALSELY_MARKED_DONE" else (
                "P2" if finding["classification"] in {"INCOMPLETE", "PROMISED BUT NOT FOUND"} else "P3"
            )
            store.create_finding(finding["mission_id"], finding["classification"], severity, requirement_ids)
    counts: dict[str, int] = {}
    for row in findings:
        counts[row["classification"]] = counts.get(row["classification"], 0) + 1
    title = "AGK RECOVERY BASELINE" if baseline else "AGK DAILY COMPLETION AUDIT"
    lines = [
        f"# {title}", "", f"Date: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"Profile boundary: `{profile}`", "", "## Executive Summary",
        f"Prompts reviewed: {prompts_reviewed}", f"Missions reviewed: {len(findings)}",
    ]
    for key in sorted(counts):
        lines.append(f"- {key}: {counts[key]}")
    lines += ["", "## Findings"]
    if not findings:
        lines.append("No persisted prompts were accessible in this profile boundary.")
    for row in findings:
        lines += [
            "", f"### {row['mission_id']}", f"Classification: **{row['classification']}**",
            f"Requirements: {len(row['requirements'])}", f"Unresolved: {len(row['unresolved'])}",
            f"Missing evidence: {len(row['missing_evidence'])}",
            "Human authorization required: " + ("YES" if row["human_required"] else "NO"),
        ]
    audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    audit_path.chmod(0o600)

    package = [
        "# AGK OPERATOR RECOVERY", "", f"Profile boundary: `{profile}`",
        "This package is executable context. New/recovered backlog work still requires explicit human authorization.",
    ]
    for row in findings:
        if row["classification"] == "COMPLETE":
            continue
        prompt_rows = store.db.execute(
            """SELECT p.* FROM prompts p JOIN mission_prompts mp ON mp.prompt_id=p.id
               WHERE mp.mission_id=? ORDER BY p.created_at""", (row["mission_id"],)
        ).fetchall()
        package += ["", f"## {row['mission_id']}", f"Classification: {row['classification']}", "", "### Original prompt(s)"]
        for prompt in prompt_rows:
            content = store.prompt_content(str(prompt["id"]))
            package += [f"#### {prompt['id']} · {prompt['source']} · session {prompt['session_id']}", "", content, ""]
        package += ["### Requirement ledger"]
        if row["requirements"]:
            for requirement in row["requirements"]:
                package.append(f"- [{requirement['status']}] {requirement['id']}: {requirement['text']}")
        else:
            package.append("- No reliable historical requirement extraction exists. Human/LLM review required.")
        package += [
            "", "### Ready-to-dispatch instruction",
            f"Review `{row['mission_id']}` against its original prompt, extract any missing requirements, "
            "check existing artifacts before creating work, require explicit human authorization, then execute only approved unresolved nodes through Verification + Gauntlet + Completeness Oracle.",
        ]
    operator_path.write_text("\n".join(package) + "\n", encoding="utf-8")
    operator_path.chmod(0o600)
    return audit_path, operator_path, findings


def audit_profile(*, profile: str, state_db: Path, completion_root: Path,
                  reports_root: Path, baseline: bool = False) -> dict[str, Any]:
    store = CompletionStore(completion_root)
    messages = _session_messages(state_db)
    imported = 0
    assistant_by_session: dict[str, list[str]] = {}
    for row in messages:
        if row["role"] == "assistant":
            assistant_by_session.setdefault(str(row["session_id"]), []).append(str(row["content"]))
            continue
        source_key = f"state.db:{profile}:message:{row['id']}"
        existed = store.db.execute("SELECT 1 FROM prompts WHERE source_key=?", (source_key,)).fetchone()
        prompt_id = store.archive_prompt(
            str(row["content"]), source=str(row["source"]), session_id=str(row["session_id"]),
            profile=profile, source_key=source_key,
            metadata={"message_id": row["id"], "timestamp": row["timestamp"], "title": row["title"]},
        )
        mission_id = _mission_for_prompt(store, prompt_id, str(row["session_id"]))
        for index, requirement in enumerate(extract_candidate_requirements(str(row["content"])), start=1):
            exists = store.db.execute(
                "SELECT 1 FROM requirements WHERE mission_id=? AND text=?", (mission_id, requirement)
            ).fetchone()
            if not exists:
                human_gate = bool(re.search(r"(?i)\b(human|CTO|owner|approval|approve|authorize)\b", requirement))
                store.add_requirement(
                    prompt_id, requirement, mission_id=mission_id, type="CANDIDATE",
                    provenance=f"user-message:{row['id']}#candidate-{index}", human_gate=human_gate,
                )
        if not existed:
            imported += 1
    for session_id, responses in assistant_by_session.items():
        promises = []
        for response in responses:
            promises.extend(extract_promises(response))
        if not promises:
            continue
        mission_id = f"HIST-{sha256_text(session_id)[:12]}"
        prompt = store.db.execute(
            "SELECT prompt_id FROM mission_prompts WHERE mission_id=? ORDER BY rowid LIMIT 1", (mission_id,)
        ).fetchone()
        if not prompt:
            continue
        for promise in promises:
            exists = store.db.execute(
                "SELECT 1 FROM requirements WHERE mission_id=? AND text=?", (mission_id, promise)
            ).fetchone()
            if not exists:
                store.add_requirement(str(prompt["prompt_id"]), promise, mission_id=mission_id,
                                      type="PROMISE", provenance=f"assistant-session:{session_id}")
    audit_path, operator_path, findings = _write_reports(
        store, profile, reports_root, baseline, len([row for row in messages if row["role"] == "user"])
    )
    result = {
        "profile": profile, "prompts_imported": imported, "prompts_reviewed": len(messages),
        "missions_reviewed": len(findings), "audit_report": str(audit_path),
        "operator_report": str(operator_path),
    }
    store.event("recovery.audit.completed", profile, result)
    store.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="AGK profile-local Recovery Auditor")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--state-db", type=Path, required=True)
    parser.add_argument("--completion-root", type=Path, required=True)
    parser.add_argument("--reports-root", type=Path, required=True)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = audit_profile(profile=args.profile, state_db=args.state_db,
                           completion_root=args.completion_root, reports_root=args.reports_root,
                           baseline=args.baseline)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"Recovery audit complete: {result['audit_report']}")
        print(f"Operator package: {result['operator_report']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
