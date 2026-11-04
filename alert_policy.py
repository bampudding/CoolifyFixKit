"""Only sustained, attributable resource incidents produce real-time alerts."""
import datetime as dt
import json
import time
from pathlib import Path
from zoneinfo import ZoneInfo

GIB=1024**3

def stamp(when):
    return dt.datetime.fromtimestamp(when,ZoneInfo('Asia/Seoul')).strftime('%m/%d %H:%M KST')

def findings(sample):
    result={'memory':[], 'io':[]}
    host=sample['host']
    owners=[('Debian',host)]+[(n.removeprefix('lxc.payload.'),v) for n,v in sample['guests'].items()]
    for name,data in owners:
        mem=data['memory_psi']['some']['avg10'];io=data['io_psi']['full']['avg10']
        if name=='Debian':
            low=host['available']<4*GIB
            detail=f"Debian: 여유 RAM {host['available']/GIB:.2f}GiB, 메모리 대기 {mem:.1f}%"
        else:
            cap=data.get('max');low=bool(cap and data['current']>=cap*.93)
            detail=f"{name}: RAM {data['current']/GIB:.2f}GiB"+(f" / {cap/GIB:.2f}GiB ({data['current']/cap*100:.1f}%)" if cap else '')+f', 메모리 대기 {mem:.1f}%'
        if low or mem>=10:result['memory'].append(detail)
        if io>=10:result['io'].append(f'{name}: I/O 전체 대기 {io:.1f}%')
    return result

def resource_context(kind,episode):
    try:
        data=json.loads(Path('/run/coolify-fixkit/top-resources.json').read_text())
        if not 0<=time.time()-data['time']<=120:return ''
        rows=data['memory'] if kind=='memory' else data['io']
        tasks=episode.setdefault('tasks',{})
        if episode.get('task_sample')!=data['time']:
            episode['task_sample']=data['time']
            for row in rows[:6]:
                if kind=='memory' and row['rss']<512*1024**2:continue
                if kind=='io' and row['read_bps']+row['write_bps']<100*1024:continue
                key=f"{row['pid']}:{row.get('start',0)}"
                entry=tasks.setdefault(key,{'samples':0,'row':row,'last':0})
                entry.update(samples=entry['samples']+1,row=row,last=data['time'])
        tasks={k:v for k,v in tasks.items() if data['time']-v['last']<120}
        episode['tasks']=tasks
        lines=[]
        for entry in tasks.values():
            if entry['samples']<3:continue
            row=entry['row'];owner=row['cgroup'].split('/')[1].removeprefix('lxc.payload.')
            if kind=='memory':
                if row['rss']<512*1024**2:continue
                value=f"RSS {row['rss']/GIB:.2f}GiB"
            else:
                if row['read_bps']+row['write_bps']<100*1024:continue
                value=f"읽기 {row['read_bps']/1024**2:.1f}, 쓰기 {row['write_bps']/1024**2:.1f}MiB/s"
            lines.append(f"- {owner} / {row['comm']} (PID {row['pid']}): {value}")
        return '\n'.join(lines[:3])
    except (OSError,ValueError,KeyError,TypeError,IndexError):return ''


class Incidents:
    def __init__(self,delivery):
        self.delivery=delivery
        self.state=delivery.data.setdefault('incident_policy',{'active':{}})
        self.last=None;self.last_save=0

    def update(self,sample):
        now=sample['time']
        if self.last is not None and now<=self.last:return
        gap=None if self.last is None else now-self.last
        self.last=now
        rows=findings(sample)
        for kind,details in rows.items():
            active=self.state['active'];episode=active.get(kind)
            if episode and episode['boot']!=sample['boot']:
                self.delivery.cancel_incident(episode['id']);active.pop(kind);episode=None
            if details:
                if episode is None:
                    episode={'id':f"{sample['boot']}:{kind}:{now}",'boot':sample['boot'],'since':now,'last':now,
                        'notified':False,'good_since':None,'reminder':now}
                    active[kind]=episode
                if gap is None or gap>20:
                    # Downtime is not evidence of continuous pressure.
                    episode['since']=now
                    episode['tasks']={}
                if episode['good_since'] is not None and not episode['notified']:episode['since']=now
                episode['last']=now;episode['good_since']=None;episode['details']=details
                top=resource_context(kind,episode)
                duration=now-episode['since'];threshold=600
                if ((not episode['notified'] and duration>=threshold) or
                    (episode['notified'] and duration>=threshold and now-episode['reminder']>=21600)):
                    title='확인 필요 — '+('장시간 메모리 사용' if kind=='memory' else '장시간 I/O 점유')
                    action=('최근 빌드의 동시 실행과 메모리 사용 상위 작업을 확인하세요.' if kind=='memory' else
                            'I/O 상위 작업과 빌드·백업 실행 시간을 확인하세요. 대기율만으로 SSD 고장을 뜻하지는 않습니다.')
                    # Notify only when the sustained condition has attributable tasks.
                    if not top:continue
                    body=f"**{title}**\n시작: {stamp(episode['since'])} / 경과: {int(duration//60)}분\n"+'\n'.join(details[:4])+f'\n동시에 반복 관측된 상위 작업(원인 확정 아님):\n{top}\n확인: {action}'
                    self.delivery.enqueue({'kind':'pressure','scope':kind,'message':body[:1200]},incident=episode['id'])
                    episode['notified']=True;episode['reminder']=now
            elif episode:
                if gap is None or gap>20:episode['good_since']=now
                if episode['good_since'] is None:episode['good_since']=now
                if now-episode['good_since']>=60:
                    self.delivery.cancel_incident(episode['id'])
                    active.pop(kind)
        # Runtime timers stay in memory; at most one routine SSD write per minute.
        if now-self.last_save>=60:
            with self.delivery.mutex:self.delivery.save()
            self.last_save=now
