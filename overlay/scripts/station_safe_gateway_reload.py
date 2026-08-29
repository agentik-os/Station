#!/usr/bin/env python3
"""Drain a Station gateway to zero work, then request an in-band reload."""
from __future__ import annotations
import argparse,json,os,pwd,re,signal,subprocess,sys,time
from pathlib import Path

HERMES_SOURCE=Path('/opt/agk-terminal/hermes-agent')

def _drain_api():
 sys.path.insert(0,str(HERMES_SOURCE))
 from gateway.drain_control import clear_drain_request, write_drain_request
 return clear_drain_request, write_drain_request


def _clear_drain_marker(clear_drain_request,home:Path)->None:
 clear_drain_request(home=home)
 if (home/'.drain_request.json').exists():
  raise RuntimeError('drain marker remains active after cleanup')


def _activate_drain_marker(write_drain_request,clear_drain_request,home:Path)->None:
 try:
  write_drain_request(principal='station-safe-reload',suppress_notification=True,home=home)
 except BaseException:
  _clear_drain_marker(clear_drain_request,home)
  raise

ALLOWED_USERS={'operator','agentik','mission','private'}
UNIT=re.compile(r'^hermes-gateway(?:-[a-z0-9-]+)?\.service$')

class StatusUnavailable(RuntimeError): pass

def status(home:Path)->dict:
 try: value=json.loads((home/'gateway_state.json').read_text(encoding='utf-8'))
 except (OSError,ValueError,TypeError) as exc: raise StatusUnavailable('gateway state unavailable') from exc
 if not isinstance(value,dict) or not isinstance(value.get('active_agents'),int) or value['active_agents']<0:
  raise StatusUnavailable('gateway active-agent count unavailable')
 return value

def user_call(user:str,uid:int,*argv:str,timeout:int=30)->subprocess.CompletedProcess:
 return subprocess.run(['/usr/bin/setpriv','--reuid',user,'--regid',user,'--clear-groups','/usr/bin/env',f'HOME=/home/{user}',f'HERMES_HOME={os.environ["TARGET_HERMES_HOME"]}',f'XDG_RUNTIME_DIR=/run/user/{uid}',f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus',*argv],text=True,capture_output=True,timeout=timeout,check=False)

def main()->int:
 parser=argparse.ArgumentParser(); parser.add_argument('--user',required=True); parser.add_argument('--unit',required=True); parser.add_argument('--hermes-home',required=True,type=Path); parser.add_argument('--timeout',type=int,default=1800); args=parser.parse_args()
 if args.user not in ALLOWED_USERS or not UNIT.fullmatch(args.unit): raise SystemExit('untrusted target')
 account=pwd.getpwnam(args.user); base=Path(account.pw_dir).resolve(); home=args.hermes_home.resolve()
 if home!=base/'.hermes' and base/'.hermes/profiles' not in home.parents: raise SystemExit('profile boundary violation')
 os.environ['TARGET_HERMES_HOME']=str(home); timeout=max(60,min(args.timeout,7200))
 probe=user_call(args.user,account.pw_uid,'/usr/bin/systemctl','--user','is-active',args.unit)
 if probe.returncode:
  print(json.dumps({'status':'not-running','user':args.user,'unit':args.unit,'active_agents_before':0,'old_pid':0,'new_pid':0}))
  return 0
 initial=status(home); old_pid=int(initial.get('pid') or 0)
 clear_drain_request,write_drain_request=_drain_api(); marker=home/'.drain_request.json'; marker_active=False
 def cancel_signal(signum, _frame):
  raise InterruptedError(f'safe reload cancelled by signal {signum}')
 signal.signal(signal.SIGTERM,cancel_signal); signal.signal(signal.SIGINT,cancel_signal)
 deadline=time.monotonic()+timeout
 try:
  while time.monotonic()<deadline:
   current=status(home)
   if current['active_agents']!=0:
    time.sleep(1)
    continue
   # Close only the zero-work cutover race. Never leave the gateway draining
   # while waiting for active work: new user turns must remain admissible.
   marker_active=True
   _activate_drain_marker(write_drain_request,clear_drain_request,home)
   try: os.chown(marker,account.pw_uid,account.pw_gid)
   except OSError: pass
   current=status(home)
   if current['active_agents']==0: break
   _clear_drain_marker(clear_drain_request,home); marker_active=False
   time.sleep(1)
  else: raise TimeoutError('active work did not drain; reload cancelled without interrupting it')
  result=user_call(args.user,account.pw_uid,'/usr/bin/systemctl','--user','reload',args.unit)
  if result.returncode: raise RuntimeError('in-band reload request failed')
  boot_deadline=time.monotonic()+180; current={}
  while time.monotonic()<boot_deadline:
   try: current=status(home)
   except StatusUnavailable:
    time.sleep(1); continue
   new_pid=int(current.get('pid') or 0)
   if new_pid and new_pid!=old_pid and current.get('gateway_state') in {'running','draining'}: break
   time.sleep(1)
  else: raise TimeoutError('gateway did not return after in-band reload')
 finally:
  if marker_active: _clear_drain_marker(clear_drain_request,home)
 print(json.dumps({'status':'reloaded','user':args.user,'unit':args.unit,'active_agents_before':int(initial.get('active_agents') or 0),'old_pid':old_pid,'new_pid':int(status(home).get('pid') or 0)}))
 return 0
if __name__=='__main__': raise SystemExit(main())
