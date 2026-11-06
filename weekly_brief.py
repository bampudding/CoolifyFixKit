"""Weekly Now Brief from existing journals; no new continuous disk history."""
import argparse
from collections import defaultdict,Counter
import datetime as dt
import json
from pathlib import Path
import re
import subprocess
from zoneinfo import ZoneInfo
from event_webhook import post_event
from disk_capacity import capacity_snapshot, capacity_text


def journal(*args,json_output=False):
    p=subprocess.Popen(['journalctl',*args,'--no-pager','-o','json' if json_output else 'cat'],stdout=subprocess.PIPE,text=True)
    try:
        for line in p.stdout:yield line
    finally:
        p.stdout.close()
        try:p.wait(timeout=3)
        except subprocess.TimeoutExpired:p.kill();p.wait()


def oom_context(seconds=604800,limit=4):
    result=[]
    try:known=set(json.loads(Path('/etc/coolify-fixkit/test-containers.json').read_text()))
    except (OSError,ValueError,TypeError):known=set()
    tests=0
    for raw in journal('_TRANSPORT=kernel','--since',f'{seconds} seconds ago','--grep','oom-kill:','-n','30',json_output=True):
        try:
            row=json.loads(raw);line=row['MESSAGE'];when=float(row['__REALTIME_TIMESTAMP'])/1e6
        except (ValueError,KeyError,TypeError):continue
        if any(ident in line for ident in known):tests+=1;continue
        task=re.search(r'(?:^|,)task=([^,\s]+)',line);pid=re.search(r'(?:^|,)pid=(\d+)',line)
        owner=re.search(r'lxc.payload.([^/,\s]+)',line)
        if task:
            when_text=dt.datetime.fromtimestamp(when,ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M')
            result.append((when,f"- {when_text} KST: {owner.group(1) if owner else '호스트'} / {task.group(1)} (PID {pid.group(1) if pid else '?'}) — 커널 OOM 종료"))
    result.sort()
    text='\n'.join(x[1] for x in result[-limit:])
    if tests:text+=f"\n알려진 검증용 OOM {tests}건은 업무 종료 기록에서 제외했습니다."
    return text.strip()


def build_brief():
    guests={};previous={};counts=Counter();samples=0;first=None;last=None;top=Counter()
    for line in journal('-u','coolify-fixkit-host','--since','7 days ago','-n','16000'):
        try:row=json.loads(line)
        except ValueError:continue
        if row.get('event')!='host-io':continue
        samples+=1;first=row['time'] if first is None else first;last=row['time']
        for name,g in row.get('guests',{}).items():
            x=guests.setdefault(name,{'peak':0,'cap':0,'write':0,'high_minutes':0})
            x['peak']=max(x['peak'],g['current']);x['cap']=g['max']
            if g['max']!='max' and g['current']>=int(g['max'])*.93:x['high_minutes']+=1
            old=previous.get(name)
            if old and old['identity']==g['identity']:
                x['write']+=max(0,int(g['io'].get('wbytes',0))-int(old['io'].get('wbytes',0)))
            previous[name]=g
        for item in row.get('top_io',[]):top[item['comm']]+=item['write_bps']+item['read_bps']
    for line in journal('-u','coolify-fixkit-discord','--since','7 days ago','-n','10000'):
        try:row=json.loads(line)
        except ValueError:continue
        if row.get('event')=='discord-delivered':counts['legacy_pressure' if row.get('kind')=='pressure' and row.get('policy_version')!=2 else row.get('kind')]+=1
    zone=ZoneInfo('Asia/Seoul');now=dt.datetime.now(zone)
    period=(dt.datetime.fromtimestamp(first,zone).strftime('%m/%d %H:%M')+' ~ '+dt.datetime.fromtimestamp(last,zone).strftime('%m/%d %H:%M')) if first else '관측 자료 없음'
    lines=[f"**이번 주 서버 운영 요약**",f"관측: {period} KST ({samples:,}개 표본)",'', '**최근 7일의 실제 종료 기록**',oom_context() or '- 보존된 커널 로그에서 OOM 종료를 찾지 못했습니다.','', '**RAM 최고 사용 / SSD 쓰기**']
    for name,x in guests.items():
        cap=f" / {int(x['cap'])/2**30:.2f}GiB" if x['cap']!='max' else ''
        lines.append(f"- {name.removeprefix('lxc.payload.')}: {x['peak']/2**30:.2f}GiB{cap}, 93% 이상 {x['high_minutes']}표본, 쓰기 {x['write']/2**30:.1f}GiB")
    try:lines+=['','**SSD 공간 현황**',capacity_text(capacity_snapshot())]
    except (OSError,ValueError):lines+=['','SSD 공간 현황: 측정하지 못했습니다.']
    lines+=['','**우선 확인**']
    if guests:
        owner=max(guests,key=lambda n:guests[n]['write'])
        lines.append(f"- 쓰기가 가장 많은 {owner.removeprefix('lxc.payload.')}의 빌드·백업 실행 이력을 확인하세요.")
    tasks=[n for n,_ in top.most_common() if n not in ('systemd','grep','journalctl','docker','incus','bash','sh')]
    if tasks:lines.append('- I/O 상위 작업 표본(원인 확정 아님): '+', '.join(tasks[:3]))
    lines+=['- 실제 서비스 응답 상태는 이 자료에서 확인하지 않았습니다.','','쓰기량은 guest 블록 카운터 차이이며 실제 NAND 쓰기량과 다릅니다. 로그 공백·교체 시 누락될 수 있습니다.']
    return '\n'.join(lines)[:1200],now.strftime('%G-W%V')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--print',action='store_true');args=parser.parse_args()
    message,week=build_brief()
    if args.print:print(message);return 0
    return 0 if post_event('weekly',week,message) else 1

if __name__=='__main__':raise SystemExit(main())
