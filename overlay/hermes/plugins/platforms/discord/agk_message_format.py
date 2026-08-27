"""Station-wide Discord reply normalization."""
from __future__ import annotations

import re

_URL=re.compile(r"https?://[^\s`<>]+",re.I)
_CODE=re.compile(r"```[\s\S]*?```|`[^`\n]+`")


def normalize_station_reply(content: str) -> str:
    text=str(content or "")
    if text.startswith(">>> "):
        text=text[4:]
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
