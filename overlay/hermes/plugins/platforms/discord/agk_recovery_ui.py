"""Discord Recovery Control Center with explicit human-gated mission decisions."""
from __future__ import annotations
import asyncio, hashlib, importlib.util, json, os, re, sqlite3, subprocess
from pathlib import Path
from typing import Callable
try:
    import discord
except ImportError:  # pragma: no cover
    discord=None

_CUSTOM_ID=re.compile(r"^agk_recovery:(RELAUNCH|BACKLOG|IGNORE|ALREADY_DONE):(FIND-[A-Za-z0-9]{8,24})$")
_SECRET_LIKE=re.compile(r"(?i)(?:token|password|secret|api[_-]?key|authorization)\s*[:=]|(?:ghp_|sk-)[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._-]{12,}|/(?:home|etc|var/lib)/\S+|\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@")

def _safe_discord_text(value: object, fallback: str) -> str:
    text=str(value or "")
    return fallback if _SECRET_LIKE.search(text) else text

def parse_recovery_custom_id(value: str):
    match=_CUSTOM_ID.fullmatch(str(value or ""))
    if not match: raise ValueError("invalid recovery component id")
    return match.group(1),match.group(2)

class RecapController:
    def __init__(self, state_db=Path("/home/operator/.hermes/state.db"),
                 completion_root=Path("/home/operator/.hermes/completion"),
                 harness_path=Path("/usr/local/lib/agk-terminal/scripts/completion_harness.py"),
                 auditor_path=Path("/usr/local/lib/agk-terminal/scripts/recovery_auditor.py"),
                 approval_root=Path("/var/lib/station/recovery/approvals"),
                 oracle_root=Path("/var/lib/station/recovery/oracle"), require_fresh_audit=True):
        self.state_db=Path(state_db); self.completion_root=Path(completion_root); self.require_fresh_audit=bool(require_fresh_audit)
        self.harness_path=Path(harness_path); self.auditor_path=Path(auditor_path); self.approval_root=Path(approval_root); self.oracle_root=Path(oracle_root)
    def _module(self):
        spec=importlib.util.spec_from_file_location("agk_recap_harness",self.harness_path)
        if spec is None or spec.loader is None: raise RuntimeError("completion harness unavailable")
        module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module); return module
    def _session(self,channel_id):
        db=sqlite3.connect(f"file:{self.state_db}?mode=ro",uri=True); db.row_factory=sqlite3.Row
        try:
            row=db.execute("SELECT id,title,chat_id,thread_id,last_activity_at FROM sessions WHERE source='discord' AND (thread_id=? OR chat_id=?) ORDER BY last_activity_at DESC LIMIT 1",(str(channel_id),str(channel_id))).fetchone()
            return dict(row) if row else None
        finally: db.close()
    @staticmethod
    def _mission_id(session_id): return "HIST-"+hashlib.sha256(str(session_id).encode()).hexdigest()[:12]
    def _audit_current_profile(self):
        if not self.auditor_path.is_file(): return not self.require_fresh_audit
        try:
            result=subprocess.run(["/usr/local/lib/agk-terminal/venv/bin/python",str(self.auditor_path),
              "--profile","operator","--state-db",str(self.state_db),"--completion-root",str(self.completion_root),
              "--reports-root",str(self.completion_root.parent/"reports")],text=True,capture_output=True,timeout=90,check=False)
            return result.returncode==0
        except (OSError,subprocess.SubprocessError):
            return False
    def build(self,channel_id):
        session=self._session(channel_id)
        if not session: return {"error":"No persisted Discord session for this channel."}
        audit_fresh=self._audit_current_profile()
        module=self._module(); store=module.CompletionStore(self.completion_root,approval_root=self.approval_root,oracle_root=self.oracle_root,profile="operator")
        try:
            mission_id=self._mission_id(session["id"])
            exists=store.db.execute("SELECT 1 FROM missions WHERE id=?",(mission_id,)).fetchone()
            if not exists: return {"error":"This conversation could not be audited yet.","session_id":session["id"],"mission_id":mission_id}
            gate=store.completion_gate(mission_id)
            prompts=store.db.execute("SELECT COUNT(*) FROM mission_prompts WHERE mission_id=?",(mission_id,)).fetchone()[0]
            verified=sum(row["status"]=="VERIFIED" for row in gate["requirements"])
            unresolved=[]
            for row in gate["unresolved"]+gate["missing_evidence"]:
                if row["id"] not in {item["id"] for item in unresolved}: unresolved.append(row)
            return {"session_id":session["id"],"session_title":session.get("title") or "Conversation",
              "mission_id":mission_id,"prompts":prompts,"requirements_total":len(gate["requirements"]),
              "verified":verified,"unfinished":len(unresolved),"blocked":sum(r["status"]=="BLOCKED" for r in gate["requirements"]),
              "human_required":len(gate["human_required"]),"missing_evidence":len(gate["missing_evidence"]),
              "audit_fresh":audit_fresh,"permit_done":gate["permit_done"] and audit_fresh,
              "classification":gate["classification"] if audit_fresh else "AUDIT_DEGRADED",
              "completion_oracle_passed":gate["completion_oracle_passed"],"integrity_error":gate.get("integrity_error") or "",
              "ledger_sha256":gate.get("ledger_sha256") or "","unresolved":unresolved,
              "systems":{"prompt_archive":prompts>0,"requirement_graph":len(gate["requirements"])>0,
                "evidence_graph":bool(store.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evidence'").fetchone()),
                "loop_graph":self.harness_path.is_file(),
                "gauntlet":Path("/home/operator/.hermes/agents/completion-oracle/workflow.yaml").is_file(),
                "completion_oracle":Path("/home/operator/.hermes/agents/completion-oracle/agent.yaml").is_file(),
                "daily_recovery":Path("/var/lib/station/recovery/index.json").is_file() and audit_fresh}}
        finally: store.close()
    def ensure_finding_for_recap(self,recap):
        if recap.get("error") or recap.get("permit_done") or not recap.get("audit_fresh"):
            raise RuntimeError("conversation has no fresh relaunchable audit")
        module=self._module(); store=module.CompletionStore(self.completion_root,approval_root=self.approval_root,oracle_root=self.oracle_root,profile="operator")
        try:
            gate=store.completion_gate(recap["mission_id"])
            if gate.get("ledger_sha256")!=recap.get("ledger_sha256"):
                raise RuntimeError("conversation graph changed; refresh recap before relaunch")
            rows=store.db.execute("SELECT id,human_decision FROM findings WHERE mission_id=? ORDER BY created_at DESC",(recap["mission_id"],)).fetchall()
            open_finding=None
            for row in rows:
                finding_id=str(row["id"]); decision=row["human_decision"]
                if decision is None and open_finding is None: open_finding=finding_id
                if decision=="RELAUNCH":
                    package=self.completion_root/"relaunch"/f"{finding_id}.json"
                    try: previous=json.loads(package.read_text(encoding="utf-8"))
                    except (OSError,ValueError,TypeError): previous={}
                    if previous.get("ledger_sha256")==gate.get("ledger_sha256"):
                        raise RuntimeError("this exact recovery graph was already relaunched")
            if open_finding: return open_finding
            ids=[item["id"] for item in recap["unresolved"]]
            severity="P1" if recap["classification"]=="FALSELY_MARKED_DONE" else "P2"
            return store.create_finding(recap["mission_id"],recap["classification"],severity,ids)
        finally: store.close()
    def ensure_finding(self,channel_id):
        return self.ensure_finding_for_recap(self.build(channel_id))


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

async def _resume_bound_session(session_id,instruction,channel=None):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}",str(session_id or "")):
        raise ValueError("invalid bound Hermes session")
    process=await asyncio.create_subprocess_exec(
        "/opt/agk-terminal/hermes-agent/venv/bin/hermes","chat","--resume",str(session_id),
        "--query-file","-","--source","discord-recovery",
        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL,
    )
    await process.communicate(str(instruction).encode("utf-8"))
    if channel is not None and hasattr(channel,"send"):
        message=(f"Recovery run completed for Hermes session `{session_id}`. Use `/recap` → Refresh for the verified graph." if process.returncode==0 else f"Recovery run for `{session_id}` stopped with a non-zero status. Use `/recap` → Refresh for evidence and blockers.")
        try: await channel.send(message)
        except Exception: pass
    return process.returncode


def _recap_embed(controller,recap):
    if recap.get("error"):
        return discord.Embed(title="Operator · Conversation Recap",description=recap["error"],color=discord.Color.red())
    state="COMPLETE" if recap["permit_done"] else recap["classification"]
    embed=discord.Embed(title="Operator · Conversation Recap",description=(f"**{state}** · `{recap['mission_id']}`\n{recap['prompts']} prompts · {recap['requirements_total']} requirements · {recap['verified']} verified · {recap['unfinished']} unfinished"),color=discord.Color.green() if recap["permit_done"] else discord.Color.orange())
    systems=recap["systems"]
    embed.add_field(name="Completion systems",value=" · ".join(f"{'✓' if value else '✗'} {name.replace('_',' ')}" for name,value in systems.items()),inline=False)
    embed.add_field(name="Review gates",value=(f"Integrity: `{'FAIL' if recap['integrity_error'] else 'PASS'}` · Oracle: `{'PASS' if recap['completion_oracle_passed'] else 'PENDING'}` · Human: `{recap['human_required']}` · Missing evidence: `{recap['missing_evidence']}`"),inline=False)
    if recap["unresolved"]:
        lines=[]
        for row in recap["unresolved"][:8]:
            text=_safe_discord_text(row.get("text"),"[Sensitive requirement withheld]")
            lines.append(f"• `{row['id']}` [{row['status']}] {text[:180]}")
        embed.add_field(name="Not done / not verified",value="\n".join(lines)[:1024],inline=False)
    else: embed.add_field(name="Remaining work",value="No unresolved requirement in the current ledger.",inline=False)
    embed.set_footer(text="Relaunch Missing is explicit owner authorization and resumes this Discord session only.")
    return embed


class RecapConfirmView(discord.ui.View if discord else object):
    def __init__(self,parent,finding_id):
        super().__init__(timeout=60); self.parent=parent; self.finding_id=finding_id
        confirm=discord.ui.Button(label="Confirm Relaunch",style=discord.ButtonStyle.danger,custom_id="agk_recap_confirm_relaunch"); confirm.callback=self._confirm; self.add_item(confirm)
        cancel=discord.ui.Button(label="Cancel",style=discord.ButtonStyle.secondary,custom_id="agk_recap_cancel"); cancel.callback=self._cancel; self.add_item(cancel)
    async def _confirm(self,interaction):
        if not await _authorized(self.parent.adapter,interaction): return
        await interaction.response.defer(ephemeral=True)
        try:
            finding=await asyncio.to_thread(self.parent.recap_controller.ensure_finding_for_recap,self.parent.recap)
            if finding!=self.finding_id: raise RuntimeError("recovery finding changed; refresh recap")
            result=await asyncio.to_thread(self.parent.recovery_controller.decide,self.finding_id,"RELAUNCH",interaction.user.id)
            if result.get("idempotent"):
                await interaction.edit_original_response(content=f"Recovery `{self.finding_id}` was already relaunched.",view=None)
                return
            instruction=(f"Owner-authorized /recap recovery for {result['mission_id']}. Load the profile-local recovery package for finding {finding}; reread every original prompt; compare requirements, artifacts, evidence, Gauntlet and Completion Oracle; execute only unfinished or unverified nodes; continue until the approved graph is closed, then return proof.")
            asyncio.create_task(_resume_bound_session(self.parent.recap["session_id"],instruction,interaction.channel))
        except Exception as exc:
            await interaction.followup.send(f"Relaunch failed safely: {type(exc).__name__}",ephemeral=True); return
        await interaction.edit_original_response(content=f"Recovery `{finding}` accepted.",view=None)
        await interaction.followup.send(f"Recovery `{finding}` authorized and relaunched in this conversation session.",ephemeral=True)
        await self.parent.reload(); await self.parent.refresh_message()
    async def _cancel(self,interaction):
        if not await _authorized(self.parent.adapter,interaction): return
        await interaction.response.edit_message(content="Recap relaunch cancelled.",view=None)


class RecapView(discord.ui.View if discord else object):
    def __init__(self,adapter,recap_controller=None,recovery_controller=None):
        super().__init__(timeout=900); self.adapter=adapter; self.recap_controller=recap_controller or RecapController(); self.recovery_controller=recovery_controller or RecoveryController(); self.recap={}; self.message=None
    async def load(self): self.recap=await asyncio.to_thread(self.recap_controller.build,self.channel_id); self._build()
    def bind_channel(self,channel_id): self.channel_id=str(channel_id)
    async def reload(self): self.recap=await asyncio.to_thread(self.recap_controller.build,self.channel_id); self._build()
    def _build(self):
        self.clear_items(); incomplete=not self.recap.get("error") and not self.recap.get("permit_done",False)
        refresh=discord.ui.Button(label="Refresh",style=discord.ButtonStyle.primary,custom_id="agk_recap_refresh"); refresh.callback=self._refresh; self.add_item(refresh)
        relaunch=discord.ui.Button(label="Relaunch Missing",style=discord.ButtonStyle.danger,custom_id="agk_recap_relaunch",disabled=not incomplete); relaunch.callback=self._relaunch; self.add_item(relaunch)
        recovery=discord.ui.Button(label="Recovery Center",style=discord.ButtonStyle.secondary,custom_id="agk_recap_recovery"); recovery.callback=self._recovery; self.add_item(recovery)
        close=discord.ui.Button(label="Close",style=discord.ButtonStyle.secondary,custom_id="agk_recap_close"); close.callback=self._close; self.add_item(close)
    async def _refresh(self,interaction):
        if not await _authorized(self.adapter,interaction): return
        await interaction.response.defer(); await self.reload(); await interaction.edit_original_response(embed=_recap_embed(self.recap_controller,self.recap),view=self)
    async def _relaunch(self,interaction):
        if not await _authorized(self.adapter,interaction): return
        try: finding=await asyncio.to_thread(self.recap_controller.ensure_finding_for_recap,self.recap)
        except Exception as exc: await interaction.response.send_message(f"Cannot relaunch safely: {type(exc).__name__}. Refresh the recap.",ephemeral=True); return
        await interaction.response.send_message("Relaunch every unfinished/unverified node in this same conversation?",view=RecapConfirmView(self,finding),ephemeral=True)
    async def _recovery(self,interaction):
        if not await _authorized(self.adapter,interaction): return
        view=RecoveryView(self.adapter,self.recovery_controller); await view.load(); await interaction.response.send_message(embed=_embed(view.controller,view.rows),view=view,ephemeral=True); view.message=await interaction.original_response()
    async def _close(self,interaction):
        if not await _authorized(self.adapter,interaction): return
        await interaction.response.edit_message(content="Recap closed.",embed=None,view=None)
    async def refresh_message(self):
        if self.message: await self.message.edit(embed=_recap_embed(self.recap_controller,self.recap),view=self)


def register_recovery_commands(adapter,tree):
    if discord is None or Path(os.environ.get("HERMES_HOME", "")).resolve() != Path("/home/operator/.hermes"):
        return False
    @tree.command(name="station-recovery",description="Review incomplete missions and authorize recovery actions")
    async def station_recovery(interaction:discord.Interaction):
        if not await _authorized(adapter,interaction): return
        view=RecoveryView(adapter); await view.load(); await interaction.response.send_message(embed=_embed(view.controller,view.rows),view=view,ephemeral=True); view.message=await interaction.original_response()
    @tree.command(name="recap",description="Audit this conversation against every prompt and completion gate")
    async def recap(interaction:discord.Interaction):
        if not await _authorized(adapter,interaction): return
        view=RecapView(adapter); view.bind_channel(interaction.channel_id)
        await interaction.response.defer(ephemeral=True); await view.load()
        await interaction.edit_original_response(embed=_recap_embed(view.recap_controller,view.recap),view=view)
        view.message=await interaction.original_response()
    return True
