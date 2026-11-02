import json, subprocess, time, sys, os
from pathlib import Path
from datetime import datetime, timezone
root=Path(__file__).resolve().parent
manifest=json.loads((root/'manifest.json').read_text())
work=Path(manifest['working_directory'])
command=['codex','--ask-for-approval','never','exec','--model',manifest['model'],'-c','model_reasoning_effort="xhigh"','--sandbox','danger-full-access','--ephemeral','--json','--disable','multi_agent','--disable','skill_search','-c','web_search="disabled"','--skip-git-repo-check','--cd',str(work),'--output-last-message',str(root/'last_message.txt'),'--ignore-user-config','-']
(root/'command.json').write_text(json.dumps(command,indent=2)+'\n')
started=time.monotonic();counts={};terminal=[]
with (root/'stderr.log').open('w') as err, (root/'events.jsonl').open('w',buffering=1) as events:
 proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,text=True,start_new_session=True)
 manifest.update(status='running',codex_pid=proc.pid,runner_pid=os.getpid())
 (root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
 proc.stdin.write((root/'prompt.txt').read_text());proc.stdin.close()
 print(json.dumps({'status':'running','pid':proc.pid,'audit_directory':str(root)}),flush=True)
 for line in proc.stdout:
  events.write(line)
  try:e=json.loads(line)
  except ValueError:continue
  kind=e.get('type','unknown');counts[kind]=counts.get(kind,0)+1
  if kind=='turn.completed':terminal.append(e.get('usage') or {})
  item=e.get('item') or {};brief={'event':kind,'item_type':item.get('type'),'seconds':round(time.monotonic()-started,1),'event_counts':counts}
  if kind in ('turn.completed','turn.failed','error'):brief['terminal_event']=e;print(json.dumps(brief,ensure_ascii=False),flush=True)
  (root/'progress.json').write_text(json.dumps(brief,ensure_ascii=False,indent=2)+'\n')
 code=proc.wait()
usage={}
for key in ['input_tokens','output_tokens','cached_input_tokens','reasoning_output_tokens']:
 values=[u.get(key) for u in terminal]
 usage[key]=sum(values) if values and all(type(v) is int and v>=0 for v in values) else None
usage['total_tokens']=usage['input_tokens']+usage['output_tokens'] if usage['input_tokens'] is not None and usage['output_tokens'] is not None else None
summary={'exit_code':code,'elapsed_seconds':round(time.monotonic()-started,2),'terminal_turns':len(terminal),'event_counts':counts,'usage':usage,'baseline_tokens':manifest['baseline_tokens'],'total_baseline_multiple':usage['total_tokens']/manifest['baseline_tokens'] if usage['total_tokens'] is not None else None}
(root/'usage_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
manifest.update(status='completed' if code==0 else 'failed',finished_at_utc=datetime.now(timezone.utc).isoformat())
(root/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
sys.exit(code)
