"""Discord Recovery Control Center with explicit human-gated mission decisions."""
from __future__ import annotations
import asyncio, json, os, re, sqlite3, subprocess
from pathlib import Path
from typing import Callable
try:
    import discord
except ImportError:  # pragma: no cover
    discord=None

_CUSTOM_ID=re.compile(r"^agk_recovery:(RELAUNCH|BACKLOG|IGNORE|ALREADY_DONE):(FIND-[A-Za-z0-9]{8,24})$")
_SECRET_LIKE=re.compile(r"(?i)(?:token|password|secret|api[_-]?key|authorization)\s*[:=]|(?:ghp_|sk-)[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._-]{12,}|/(?:home|etc|var/lib)/\S+")

def _safe_discord_text(value: object, fallback: str) -> str:
    text=str(value or "")
    return fallback if _SECRET_LIKE.search(text) else text

def parse_recovery_custom_id(value: str):
    match=_CUSTOM_ID.fullmatch(str(value or ""))
    if not match: raise ValueError("invalid recovery component id")
    return match.group(1),match.group(2)

class RecoveryController:
    def __init__(self, completion_root=Path("/home/operator/.hermes/completion"), fleet_index=Path("/var/lib/station/recovery/index.json"), runner:Callable|None=None):
        self.completion_root=Path(completion_root); self.fleet_index=Path(fleet_index); self.runner=runner or subprocess.run
    def list_findings(self):
        db_path=self.completion_root/"completion.db"
        if not db_path.is_file(): return []
        db=sqlite3.connect(f"file:{db_path}?mode=ro",uri=True); db.row_factory=sqlite3.Row
        try:
            rows=db.execute("""SELECT f.id finding_id,f.mission_id,f.classification,f.severity,f.human_decision,m.project,m.state FROM findings f JOIN missions m ON m.id=f.mission_id WHERE f.human_decision IS NULL ORDER BY CASE f.severity WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,f.created_at LIMIT 100""").fetchall()
            return [dict(row) for row in rows]
        finally: db.close()
    def fleet_summary(self):
        try: return json.loads(self.fleet_index.read_text()).get("summary",{})
        except (OSError,ValueError,AttributeError): return {}
    def safe_mission_context(self,finding_id):
        parse_recovery_custom_id(f"agk_recovery:RELAUNCH:{finding_id}")
        path=self.completion_root/"relaunch"/f"{finding_id}.json"
        payload=json.loads(path.read_text(encoding="utf-8"))
        lines=[f"# RECOVERY MISSION {payload['mission_id']}","",f"Finding: `{finding_id}`","","## Original request"]
        for prompt in payload.get("original_prompts",[]):
            content=_safe_discord_text(prompt.get("content"),"[Original prompt withheld from Discord: sensitive material detected. Load it inside the owning Operator profile.]")
            lines.extend(["",content[:12000]])
        lines.extend(["","## Approved unresolved requirements"])
        for requirement in payload.get("requirements",[]):
            text=_safe_discord_text(requirement.get("text"),"[Requirement text withheld: sensitive material detected]")
            lines.append(f"- [{requirement.get('status')}] {requirement.get('id')}: {text[:1000]}")
        lines.extend(["","Execute only approved unresolved nodes. Return artifacts, verification evidence, Gauntlet and Completion Oracle verdict."])
        return "\n".join(lines)
    def decide(self,finding_id,decision,actor):
        parse_recovery_custom_id(f"agk_recovery:{decision}:{finding_id}")
        result=self.runner(["sudo","/usr/local/lib/agk-terminal/venv/bin/python","/usr/local/lib/agk-terminal/scripts/recovery_router.py",finding_id,decision,"--actor",str(actor),"--source","discord"],text=True,capture_output=True,timeout=60,check=False)
        if result.returncode!=0: raise RuntimeError("Recovery decision could not be persisted")
        return json.loads(result.stdout)

async def _authorized(adapter,interaction):
    return await adapter._check_slash_authorization(interaction,"/station-recovery")

def _embed(controller,rows,selected=""):
    summary=controller.fleet_summary(); embed=discord.Embed(title="Station · Completion Recovery",description=(f"Daily independent completeness control\n**{summary.get('prompts_reviewed',0)}** prompts · **{summary.get('missions_reviewed',0)}** missions · **{len(rows)}** Operator findings awaiting a human decision"),color=discord.Color.orange())
    row=next((item for item in rows if item["finding_id"]==selected),None)
    if row: embed.add_field(name=f"{row['severity']} · {row['mission_id']}",value=f"Classification: `{row['classification']}`\nProject: `{row.get('project') or 'unknown'}`\nDecision required. Viewing does not authorize execution.",inline=False)
    embed.set_footer(text="RELAUNCH is an explicit work authorization. Verified work is never duplicated.")
    return embed

class RecoveryConfirmView(discord.ui.View if discord else object):
    def __init__(self,parent,decision,finding_id):
        super().__init__(timeout=60); self.parent=parent; self.decision=decision; self.finding_id=finding_id
        confirm=discord.ui.Button(label=f"Confirm {decision}",style=discord.ButtonStyle.danger if decision in {"RELAUNCH","IGNORE"} else discord.ButtonStyle.success,custom_id=f"agk_recovery:{decision}:{finding_id}")
        confirm.callback=self._confirm; cancel=discord.ui.Button(label="Cancel",style=discord.ButtonStyle.secondary,custom_id="agk_recovery_cancel"); cancel.callback=self._cancel; self.add_item(confirm); self.add_item(cancel)
    async def _confirm(self,interaction):
        if not await _authorized(self.parent.adapter,interaction): return
        await interaction.response.defer(ephemeral=True)
        try: result=await asyncio.to_thread(self.parent.controller.decide,self.finding_id,self.decision,interaction.user.id)
        except Exception as exc: await interaction.followup.send(f"Decision failed safely: {type(exc).__name__}",ephemeral=True); return
        thread_url=""
        if self.decision=="RELAUNCH":
            try:
                context=await asyncio.to_thread(self.parent.controller.safe_mission_context,self.finding_id)
                channel=interaction.channel
                if isinstance(channel,discord.Thread): thread=channel
                else: thread=await channel.create_thread(name=f"recovery-{result['mission_id']}"[:90],auto_archive_duration=1440)
                for start in range(0,len(context),1900): await thread.send(context[start:start+1900])
                thread_url=f" https://discord.com/channels/{interaction.guild_id}/{thread.id}"
            except Exception:
                thread_url=" Mission context remains available inside the owning profile."
        await interaction.followup.send(f"{self.decision} recorded for `{result['mission_id']}` in `{result['profile']}`. Operator dispatch metadata created; original prompt remains inside its owning profile.{thread_url}",ephemeral=True)
        await self.parent.reload(); await self.parent.refresh_message()
    async def _cancel(self,interaction):
        if not await _authorized(self.parent.adapter,interaction): return
        await interaction.response.edit_message(content="Recovery decision cancelled.",view=None)

class RecoveryView(discord.ui.View if discord else object):
    def __init__(self,adapter,controller=None): super().__init__(timeout=900); self.adapter=adapter; self.controller=controller or RecoveryController(); self.rows=[]; self.selected=""; self.message=None
    async def load(self): self.rows=await asyncio.to_thread(self.controller.list_findings); self._build()
    async def reload(self): await self.load()
    def _build(self):
        self.clear_items()
        if self.rows:
            options=[discord.SelectOption(label=f"{r['severity']} · {r['mission_id']}"[:100],value=r['finding_id'],description=r['classification'][:100],default=r['finding_id']==self.selected) for r in self.rows[:25]]
            select=discord.ui.Select(placeholder="Choose an incomplete mission…",options=options,custom_id="agk_recovery_select"); select.callback=self._select; self.add_item(select)
        for label,decision,style in [("Relaunch Mission","RELAUNCH",discord.ButtonStyle.danger),("Keep Backlog","BACKLOG",discord.ButtonStyle.secondary),("Already Done","ALREADY_DONE",discord.ButtonStyle.success),("Ignore","IGNORE",discord.ButtonStyle.secondary)]:
            button=discord.ui.Button(label=label,style=style,custom_id=f"agk_recovery_action_{decision.lower()}",disabled=not bool(self.selected),row=1); button.callback=lambda interaction,d=decision:self._decision(interaction,d); self.add_item(button)
        refresh=discord.ui.Button(label="Refresh",style=discord.ButtonStyle.primary,custom_id="agk_recovery_refresh",row=2); refresh.callback=self._refresh; self.add_item(refresh)
        close=discord.ui.Button(label="Close",style=discord.ButtonStyle.secondary,custom_id="agk_recovery_close",row=2); close.callback=self._close; self.add_item(close)
    async def _select(self,interaction):
        if not await _authorized(self.adapter,interaction): return
        self.selected=interaction.data["values"][0]; self._build(); await interaction.response.edit_message(embed=_embed(self.controller,self.rows,self.selected),view=self)
    async def _decision(self,interaction,decision):
        if not await _authorized(self.adapter,interaction): return
        if not self.selected: await interaction.response.send_message("Select a finding first.",ephemeral=True); return
        await interaction.response.send_message(f"Confirm `{decision}` for `{self.selected}`?",view=RecoveryConfirmView(self,decision,self.selected),ephemeral=True)
    async def _refresh(self,interaction):
        if not await _authorized(self.adapter,interaction): return
        await interaction.response.defer(); await self.reload(); await interaction.edit_original_response(embed=_embed(self.controller,self.rows,self.selected),view=self)
    async def _close(self,interaction):
        if not await _authorized(self.adapter,interaction): return
        await interaction.response.edit_message(content="Recovery panel closed.",embed=None,view=None)
    async def refresh_message(self):
        if self.message: await self.message.edit(embed=_embed(self.controller,self.rows,self.selected),view=self)

def register_recovery_commands(adapter,tree):
    if discord is None or Path(os.environ.get("HERMES_HOME", "")).resolve() != Path("/home/operator/.hermes"):
        return False
    @tree.command(name="station-recovery",description="Review incomplete missions and authorize recovery actions")
    async def station_recovery(interaction:discord.Interaction):
        if not await _authorized(adapter,interaction): return
        view=RecoveryView(adapter); await view.load(); await interaction.response.send_message(embed=_embed(view.controller,view.rows),view=view,ephemeral=True); view.message=await interaction.original_response()
    return True
