#!/usr/bin/env python3
"""Persist non-secret Discord inter-agent ingress gates without reading secrets aloud."""
from __future__ import annotations
import argparse
from pathlib import Path

VALUES={
 "DISCORD_ALLOW_BOTS":"mentions",
 "DISCORD_BOTS_REQUIRE_INLINE_MENTION":"true",
 "DISCORD_ALLOWED_USERS":"1441423462492016821,1541816910587625492,1541817649661747351,1541817976586637382,1541817162241540126,1541131574509314209",
}

def update(path:Path)->None:
 rows=path.read_text(encoding="utf-8").splitlines() if path.exists() else []
 out=[]; seen=set()
 for row in rows:
  key=row.split("=",1)[0].strip() if "=" in row and not row.lstrip().startswith("#") else ""
  if key in VALUES: out.append(f"{key}={VALUES[key]}"); seen.add(key)
  else: out.append(row)
 for key,value in VALUES.items():
  if key not in seen: out.append(f"{key}={value}")
 path.parent.mkdir(parents=True,exist_ok=True); path.write_text("\n".join(out)+"\n",encoding="utf-8"); path.chmod(0o600)

def main()->int:
 parser=argparse.ArgumentParser(); parser.add_argument("env_file",type=Path); args=parser.parse_args(); update(args.env_file); return 0

if __name__=="__main__": raise SystemExit(main())
