#!/usr/bin/env python3
"""Deterministic state and privacy boundary for Collective automations."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

TERMS_VERSION = "partnerships-v2-2026-08-29"
EXPECTED_PAYMENT_LINK = "plink_1U7juxHfwsV7ya4QoqJyHjNX"
EXPECTED_PRICE = "price_1U7juWHfwsV7ya4Qjl7FLMzU"
DISCORD_ID = re.compile(r"^[1-9][0-9]{16,20}$")
SIGNATURE_STEPS = ("house", "deals")

INTRO_FIELDS = {
    "CzPLfPs3glaR": "discord",
    "S7TU0edWCcBO": "name",
    "GLzWCSGkkqIw": "building",
    "8KaT1Jlqxfkv": "audience",
    "K4dzvbmcUegX": "collective",
    "E36EiKHgYhPf": "timezone",
}
DEAL_PUBLIC_FIELDS = {
    "txCny9WIhB6C": "discord",
    "6GuyfdpuwbDv": "role",
    "2QcSq6xhvSDo": "deal_type",
    "E7i07POOiF88": "name",
    "IMd6gatnSQYN": "need",
    "4PON0l0QgCIi": "ticket",
    "g4IEGj0fgwMd": "referrer",
    "C5rIqlBgL9yI": "operator",
    "vO6YB81f3Mw1": "accepted",
}
PRIVATE_DEAL_FIELDS = {"e1PGhlvFlDpf"}


def utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def payload_hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _answer_value(answer: dict[str, Any]) -> Any:
    kind = str(answer.get("type") or "")
    if kind in {"text", "email", "url", "date"}:
        return answer.get(kind)
    if kind == "boolean":
        return answer.get("boolean") is True
    if kind == "number":
        return answer.get("number")
    if kind == "choice":
        choice = answer.get("choice") or {}
        return choice.get("label") or choice.get("other")
    if kind == "choices":
        choices = answer.get("choices") or {}
        return choices.get("labels") or []
    return None


def _mapped_answers(response: dict[str, Any], field_map: dict[str, str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for answer in response.get("answers") or []:
        if not isinstance(answer, dict):
            continue
        field = answer.get("field") or {}
        field_id = str(field.get("id") or "")
        key = field_map.get(field_id)
        if not key:
            continue
        value = _answer_value(answer)
        if isinstance(value, str):
            value = value.strip()[:1000]
        result[key] = value
    return result


def map_intro_response(response: dict[str, Any]) -> dict[str, Any]:
    response_id = str(response.get("response_id") or response.get("token") or "")
    if not response_id:
        raise ValueError("Typeform intro response has no stable identifier")
    values = _mapped_answers(response, INTRO_FIELDS)
    lines = ["# New Collective introduction"]
    if values.get("discord"):
        lines.append(f"Discord · {values['discord']}")
    if values.get("name"):
        lines.append(f"Name · {values['name']}")
    if values.get("building"):
        lines.extend(["", "**Building now**", str(values["building"])])
    if values.get("audience"):
        lines.extend(["", "**For**", str(values["audience"])])
    if values.get("collective"):
        lines.extend(["", "**Looking for**", str(values["collective"])])
    if values.get("timezone"):
        lines.extend(["", f"Timezone · {values['timezone']}"])
    return {
        "event_id": f"typeform:intro:{response_id}",
        "content": "\n".join(lines)[:1900],
        "payload_hash": payload_hash(values),
    }


def map_deal_response(response: dict[str, Any]) -> dict[str, Any]:
    response_id = str(response.get("response_id") or response.get("token") or "")
    if not response_id:
        raise ValueError("Typeform deal response has no stable identifier")
    values = _mapped_answers(response, DEAL_PUBLIC_FIELDS)
    accepted = values.get("accepted") is True
    lines = ["# New deal proposal"]
    if values.get("name"):
        lines.append(str(values["name"]))
    for label, key in (("Type", "deal_type"), ("Role", "role"), ("Ticket", "ticket")):
        if values.get(key):
            lines.append(f"{label} · {values[key]}")
    if values.get("need"):
        lines.extend(["", "**Need**", str(values["need"])])
    if values.get("referrer"):
        lines.append(f"Referrer · {values['referrer']}")
    if values.get("operator"):
        lines.append(f"Operator · {values['operator']}")
    lines.extend(["", "Split accepted · " + ("yes" if accepted else "no — review required")])
    return {
        "event_id": f"typeform:deal:{response_id}",
        "content": "\n".join(lines)[:1900],
        "accepted_split": accepted,
        "payload_hash": payload_hash(values),
    }


def map_paid_checkout(session: dict[str, Any], line_items: list[dict[str, Any]]) -> dict[str, str] | None:
    if session.get("payment_status") != "paid" or session.get("status") != "complete":
        return None
    if session.get("payment_link") != EXPECTED_PAYMENT_LINK or session.get("mode") != "subscription" or session.get("currency") != "eur":
        return None
    if not isinstance(session.get("amount_total"), int) or session["amount_total"] <= 0:
        return None
    prices = [
        (str((item.get("price") or {}).get("id") or ""), int(item.get("quantity") or 0))
        for item in line_items
        if isinstance(item, dict)
    ]
    if prices != [(EXPECTED_PRICE, 1)]:
        return None
    discord_id = str(session.get("client_reference_id") or (session.get("metadata") or {}).get("discord_id") or "")
    if not DISCORD_ID.fullmatch(discord_id):
        return None
    session_id = str(session.get("id") or "")
    if not session_id:
        return None
    return {
        "event_id": f"stripe:checkout:{session_id}",
        "discord_id": discord_id,
        "session_id": session_id,
        "payload_hash": payload_hash({"session_id": session_id, "discord_id": discord_id, "paid": True}),
    }


class CollectiveStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._init()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _init(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS acceptances (
                    discord_id TEXT PRIMARY KEY,
                    terms_version TEXT NOT NULL,
                    legal_name_hash TEXT,
                    house_at TEXT,
                    deals_at TEXT,
                    signed_at TEXT,
                    source TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    channel_id TEXT,
                    message_id TEXT,
                    error_class TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS welcomed (
                    discord_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    welcomed_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS legacy_signed (
                    discord_id TEXT PRIMARY KEY,
                    protected_at TEXT NOT NULL,
                    source TEXT NOT NULL
                );
                """
            )
        self.path.chmod(0o600)

    def mark_signature_step(self, discord_id: str, step: str, event_id: str) -> dict[str, Any]:
        if not DISCORD_ID.fullmatch(str(discord_id)) or step not in SIGNATURE_STEPS:
            raise ValueError("invalid signature step")
        now = utcnow()
        column = "house_at" if step == "house" else "deals_at"
        with self.connect() as db:
            db.execute(
                "INSERT INTO acceptances(discord_id,terms_version,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(discord_id) DO NOTHING",
                (discord_id, TERMS_VERSION, now),
            )
            current = db.execute(
                "SELECT terms_version FROM acceptances WHERE discord_id=?", (discord_id,)
            ).fetchone()
            if current and current["terms_version"] != TERMS_VERSION:
                db.execute(
                    "UPDATE acceptances SET terms_version=?,legal_name_hash=NULL,house_at=NULL,deals_at=NULL,signed_at=NULL,source=NULL,updated_at=? WHERE discord_id=?",
                    (TERMS_VERSION, now, discord_id),
                )
            db.execute(
                f"UPDATE acceptances SET {column}=COALESCE({column},?),terms_version=?,updated_at=? WHERE discord_id=?",
                (now, TERMS_VERSION, now, discord_id),
            )
            db.execute(
                "INSERT OR IGNORE INTO events(event_id,kind,payload_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (event_id, f"signature_{step}", payload_hash({"discord_id": discord_id, "step": step}), "delivered", now, now),
            )
        return self.signature_progress(discord_id)

    def complete_signature(self, discord_id: str, legal_name: str, phrase: str, event_id: str) -> dict[str, Any]:
        if not DISCORD_ID.fullmatch(str(discord_id)):
            raise ValueError("invalid Discord ID")
        existing = self.event_status(event_id)
        if existing and existing["status"] == "delivered":
            return {"ok": True, "discord_id": discord_id, "terms_version": TERMS_VERSION}
        progress = self.signature_progress(discord_id)
        if phrase.strip() != "I ACCEPT" or set(progress["steps"]) != set(SIGNATURE_STEPS):
            return {"ok": False, "missing": [x for x in SIGNATURE_STEPS if x not in progress["steps"]]}
        name = legal_name.strip()
        if not 2 <= len(name) <= 80:
            return {"ok": False, "missing": [], "reason": "invalid_name"}
        now = utcnow()
        with self.connect() as db:
            db.execute(
                "UPDATE acceptances SET legal_name_hash=?,signed_at=COALESCE(signed_at,?),source='modal_i_accept',terms_version=?,updated_at=? WHERE discord_id=?",
                (hashlib.sha256(name.encode("utf-8")).hexdigest(), now, TERMS_VERSION, now, discord_id),
            )
            db.execute(
                "INSERT OR IGNORE INTO events(event_id,kind,payload_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (event_id, "signature_complete", payload_hash({"discord_id": discord_id, "terms_version": TERMS_VERSION}), "delivered", now, now),
            )
            db.execute("DELETE FROM legacy_signed WHERE discord_id=?", (discord_id,))
        return {"ok": True, "discord_id": discord_id, "terms_version": TERMS_VERSION}

    def record_reaction_redirect(self, discord_id: str, event_id: str) -> None:
        self.claim_event(event_id, "reaction_redirect", payload_hash({"discord_id": discord_id}))
        self.mark_delivered(event_id, "dm", "redirect")

    def signature_progress(self, discord_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM acceptances WHERE discord_id=?", (discord_id,)).fetchone()
        if row is None:
            return {"discord_id": discord_id, "steps": [], "signed": False, "terms_version": TERMS_VERSION}
        if row["terms_version"] != TERMS_VERSION:
            return {"discord_id": discord_id, "steps": [], "signed": False, "terms_version": TERMS_VERSION}
        steps = [step for step, column in (("house", "house_at"), ("deals", "deals_at")) if row[column]]
        return {"discord_id": discord_id, "steps": steps, "signed": bool(row["signed_at"]), "terms_version": row["terms_version"]}

    def claim_event(self, event_id: str, kind: str, digest: str) -> bool:
        now = utcnow()
        with self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO events(event_id,kind,payload_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (event_id, kind, digest, "claimed", now, now),
            )
            if cursor.rowcount == 1:
                return True
            existing = db.execute(
                "SELECT status,payload_hash FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if existing and existing["status"] == "failed" and existing["payload_hash"] == digest:
                db.execute(
                    "UPDATE events SET status='claimed',error_class=NULL,updated_at=? WHERE event_id=?",
                    (now, event_id),
                )
                return True
            return False

    def mark_delivered(self, event_id: str, channel_id: str, message_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE events SET status='delivered',channel_id=?,message_id=?,error_class=NULL,updated_at=? WHERE event_id=?",
                (channel_id, message_id, utcnow(), event_id),
            )

    def mark_failed(self, event_id: str, error: BaseException) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE events SET status='failed',error_class=?,updated_at=? WHERE event_id=?",
                (type(error).__name__, utcnow(), event_id),
            )

    def event_status(self, event_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def was_welcomed(self, discord_id: str) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM welcomed WHERE discord_id=?", (discord_id,)).fetchone() is not None

    def mark_welcomed(self, discord_id: str, message_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO welcomed(discord_id,message_id,welcomed_at) VALUES(?,?,?)",
                (discord_id, message_id, utcnow()),
            )

    def signed_discord_ids(self) -> list[str]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT discord_id FROM acceptances WHERE signed_at IS NOT NULL AND terms_version=? ORDER BY discord_id",
                (TERMS_VERSION,),
            ).fetchall()
        return [str(row["discord_id"]) for row in rows]

    def protect_legacy_signed(self, discord_ids: list[str] | set[str]) -> int:
        now = utcnow()
        added = 0
        with self.connect() as db:
            for discord_id in sorted(set(discord_ids)):
                if not DISCORD_ID.fullmatch(str(discord_id)):
                    continue
                cursor = db.execute(
                    "INSERT OR IGNORE INTO legacy_signed(discord_id,protected_at,source) VALUES(?,?,?)",
                    (str(discord_id), now, "discord_role_baseline"),
                )
                added += cursor.rowcount
        return added

    def legacy_signed_ids(self) -> list[str]:
        with self.connect() as db:
            rows = db.execute("SELECT discord_id FROM legacy_signed ORDER BY discord_id").fetchall()
        return [str(row["discord_id"]) for row in rows]
