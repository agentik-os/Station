"""Hermes tool client for the UID-authenticated Station inter-agent broker."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path

from tools.registry import tool_error, tool_result

SOCKET_PATH = Path("/run/agk-station/interagent.sock")

INTERAGENT_TOOL_SCHEMA = {
    "name": "station_interagent",
    "description": "Send and receive explicit non-secret team messages across Station agents through the Operator-administered broker.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "send", "inbox", "ack"]},
            "target": {"type": "string", "enum": ["operator", "agentik", "mission", "private", "collective"]},
            "message": {"type": "string", "description": "Non-secret team message (1-4000 characters)."},
            "message_id": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["action"],
    },
}


def broker_available() -> bool:
    return SOCKET_PATH.exists()


def request(payload: dict) -> dict:
    raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
    if len(raw) > 16384:
        raise ValueError("inter-agent request too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(15)
        client.connect(str(SOCKET_PATH))
        client.sendall(raw)
        response = b""
        while not response.endswith(b"\n") and len(response) <= 65536:
            chunk = client.recv(8192)
            if not chunk: break
            response += chunk
    value = json.loads(response or b"{}")
    if not isinstance(value, dict):
        raise ValueError("invalid broker response")
    return value


def handle_interagent(args: dict, **_kwargs) -> str:
    action = str(args.get("action") or "").strip().lower()
    payload = {"action": action}
    for key in ("target", "message", "message_id", "limit"):
        if args.get(key) is not None:
            payload[key] = args[key]
    try:
        result = request(payload)
    except Exception as exc:
        return tool_error(f"Station inter-agent broker unavailable: {type(exc).__name__}")
    if not result.get("success"):
        return tool_error(str(result.get("error") or "inter-agent request failed"))
    return tool_result(result)


def interagent_prompt(_session_info: dict | None = None) -> str:
    return (
        "Station team communication: Operator, MISSION, Agentik, Private and Collective can contact "
        "one another with the station_interagent tool. Operator is the global administrator. Send only "
        "explicit non-secret messages; the broker never grants access to another Linux user's files, "
        "memory or private state. Use send for handoffs/questions, inbox to read your own queue, and ack "
        "after processing. Every peer request MUST use this tool: never write a peer bot prompt as your own "
        "assistant reply and never inject it into an agent's active/main session. The broker queues at most three "
        "concurrent deliveries, creates a dedicated Discord thread, and sends a real source-bot message with an "
        "inline target-bot mention; the target acknowledges and works only in that thread. Do not claim "
        "cross-Station communication is unavailable while this tool is ready."
    )
