"""Station-wide Discord reply normalization."""
from __future__ import annotations

import re

_URL=re.compile(r"https?://[^\s`<>]+",re.I)
_CODE=re.compile(r"```[\s\S]*?```|`[^`\n]+`")


def utf16_len(text: str) -> int:
    return len(str(text or "").encode("utf-16-le")) // 2


def truncate_station_text(text: str, limit: int) -> str:
    """Return a prefix bounded by Discord's UTF-16 unit accounting."""
    value=str(text or "")
    budget=max(0, int(limit))
    if utf16_len(value) <= budget:
        return value
    marker="…" if budget else ""
    budget=max(0, budget - utf16_len(marker))
    out=[]
    used=0
    for char in value:
        units=utf16_len(char)
        if used + units > budget:
            break
        out.append(char)
        used += units
    return "".join(out) + marker


def append_station_status(
    content: str, heading: str, detail: str, *, limit: int = 2000
) -> str:
    """Append an intact status block while trimming the prior body first."""
    suffix=f"\n\n{heading}\n{detail}"
    suffix=truncate_station_text(suffix, limit)
    body_budget=max(0, int(limit) - utf16_len(suffix))
    return truncate_station_text(str(content or ""), body_budget) + suffix


def normalize_station_reply(content: str) -> str:
    text=str(content or "")
    # Discord's leading ``>>>`` marker turns the entire message into a tinted
    # blockquote with an accent rail. Station ordinary replies are plain text;
    # tolerate incidental leading whitespace while leaving embedded evidence
    # quotes untouched.
    text=re.sub(r"\A\s*>>>[ \t]?", "", text, count=1)
    protected=[]
    for match in _CODE.finditer(text):
        protected.extend(_URL.findall(match.group(0)))
    clickable=set(re.findall(r"<(?P<url>https?://[^>]+)>|\[[^\]]+\]\((?P<md>https?://[^)]+)\)",text,re.I))
    existing={value for pair in clickable for value in pair if value}
    links=[]
    for url in protected:
        clean=url.rstrip(".,;:!?")
        if clean not in existing and clean not in links: links.append(clean)
    if links:
        text=text.rstrip()+"\n\nLinks: "+" · ".join(f"<{url}>" for url in links)
    return text
