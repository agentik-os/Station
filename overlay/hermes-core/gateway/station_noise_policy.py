"""Station-specific gateway noise policy for human-facing Discord channels."""
from __future__ import annotations

import re
from typing import Any

_AUTOMATIC_COMPRESSION_NOISE = re.compile(
    r"(?:"
    r"context\s+compression\s+timed\s+out\s+after\s+[\d.]+s\s+with\s+no\s+output\s+from\s+the\s+summary\s+model"
    r"|context\s+compression\s+is\s+temporarily\s+unavailable.*no\s+summary\s+output\s+after"
    r"|context\s+is\s+over\s+the\s+compression\s+threshold.*compression\s+is\s+currently\s+blocked"
    r")",
    re.IGNORECASE | re.DOTALL,
)


def _platform_name(platform: Any) -> str:
    value = getattr(platform, "value", platform)
    return str(value or "").strip().casefold()


def suppress_automatic_compression_notice(platform: Any, message: str) -> bool:
    """Hide only automatic compression diagnostics on Discord.

    Manual ``/compress`` feedback and provider/authentication failures remain
    visible. Local/CLI surfaces retain the full diagnostic stream.
    """

    return _platform_name(platform) == "discord" and bool(
        _AUTOMATIC_COMPRESSION_NOISE.search(str(message or ""))
    )
