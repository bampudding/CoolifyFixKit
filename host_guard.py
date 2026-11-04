#!/usr/bin/env python3
"""Native Debian cgroup-v2 guard: preserve Incus hard limits and IOPS.

Incus 7.0.1 accepts either B/s or IOPS, so apply B/s as a separate kernel
control and reconcile it after Incus device updates. No app restarts/kills.
"""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from event_webhook import post_event
from monitor_runtime import StopFlag, METRICS, publish, notify_watchdog
from resource_guard import snapshot

MIB = 1024**2


def pairs(text):
    return {k: int(v) for k,v in (line.split() for line in text.splitlines())}


def io_values(text, device):
    for line in text.splitlines():
        fields = line.split()
        if fields[0] == device:
            return dict(x.split('=',1) for x in fields[1:])
    return {}


def desired_io(current, read_mib, write_mib):
    # A partial io.max write preserves riops/wiops and unrelated devices.
    return {key: str(value*MIB) for key,value in (('rbps',read_mib),('wbps',write_mib))
            if current.get(key,'max') != str(value*MIB)}


def desired_high(limit, ratio):
    if limit == 'max':
        raise ValueError('finite Incus hard memory limit required')
    size = int(limit)
    return str(int(size*ratio)//4096*4096)


def root_device():
    device = subprocess.run(['findmnt','-n','-o','MAJ:MIN','/'],check=True,
                            capture_output=True,text=True,timeout=3).stdout.strip()
    if not re.fullmatch(r'\d+:\d+',device):
        raise ValueError('unexpected root device')
    path = Path('/sys/dev/block',device).resolve()
    # Btrfs reports a synthetic filesystem dev_t (0:N) in mountinfo. Resolve
    # its physical source partition instead, without needing block-device access.
    if not path.exists():
        source=subprocess.run(['findmnt','-n','-o','SOURCE','/'],check=True,
                              capture_output=True,text=True,timeout=3).stdout.strip().split('[',1)[0]
        if not source.startswith('/dev/') or not re.fullmatch(r'[A-Za-z0-9_.-]+',Path(source).name):
            raise ValueError('unsupported root source')
        path=Path('/sys/class/block',Path(source).name).resolve()
    if (path/'partition').exists():
        path=path.parent
    if (path/'queue/rotational').read_text().strip() != '0':
        raise ValueError('root device is not an SSD')
    return (path/'dev').read_text().strip(), path.name


def write_control(path, text):
    fd=os.open(path,os.O_WRONLY|os.O_NONBLOCK)
    try:
        os.write(fd,text.encode())
    finally:
        os.close(fd)


def validate(policy):
    if set(policy)-{'instances','oom_protection'} or not isinstance(policy.get('instances'),dict):
        raise ValueError('invalid policy')
    for name,p in policy['instances'].items():
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',name):
            raise ValueError('invalid instance name')
        if set(p)-{'read_mib','write_mib','memory_high_ratio'} or not {'read_mib','write_mib'} <= set(p):
            raise ValueError('invalid instance controls')
        for k in ('read_mib','write_mib'):
            if type(p[k]) is not int or not 1<=p[k]<=1024:
                raise ValueError('invalid bandwidth')
        if 'memory_high_ratio' in p and not 0.5<=p['memory_high_ratio']<=0.95:
            raise ValueError('invalid memory high ratio')
    protection=policy.get('oom_protection',{})
    allowed={'instances','units','containers','critical_score_adj','build_score_adj','memory_low_mib'}
    if set(protection)-allowed:
        raise ValueError('invalid OOM protection')
    for key in ('instances','units','containers'):
        if not isinstance(protection.get(key,[]),list) or any(
            not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',v)
            for v in protection.get(key,[])):
            raise ValueError('invalid protection target')
    for key,low,high,default in (('critical_score_adj',-999,-1,-950),('build_score_adj',1,1000,500),
                                 ('memory_low_mib',1,8192,4096)):
        value=protection.get(key,default)
        if type(value) is not int or not low<=value<=high:
            raise ValueError('invalid protection score/budget')
    return policy


def in_group(path,group):
    return path==group or path.startswith(group+'/')


def priority(current,critical,build,critical_score=-950,build_score=500):
    if critical:
        return min(current,critical_score)
    if build and current!=-1000:
        return max(current,build_score)
    return current


def is_build(comm,args=b''):
    if re.fullmatch(r'(?:clang(?:\+\+)?(?:-\d+)?|cc1|cc1plus|rustc|cargo|ninja|make|gmake|ccache)',comm):
        return True
    return comm=='java' and any(x in args for x in (
        b'org.gradle.launcher.daemon.bootstrap.GradleDaemon',b'org.jetbrains.kotlin.daemon.KotlinCompileDaemon'))


def critical_container_groups(names):
    groups=[]
    for name in names:
        completed=subprocess.run(['docker','inspect','--type','container','--format','{{.Id}}',name],
            capture_output=True,text=True,timeout=3,check=False)
        ident=completed.stdout.strip()
        if completed.returncode or not re.fullmatch(r'[a-f0-9]{64}',ident):
            # Keep command output (potential credentials/URLs) out of logs.
            reason='invalid-identity'
            if completed.returncode:
                error=completed.stderr.lower()
                reason=next((key for key,text in (
                    ('thread-budget','failed to create new os thread'),
                    ('thread-budget','pthread_create failed'),
                    ('target-missing','no such'),('daemon-unavailable','cannot connect'),
                    ('permission-denied','permission denied')) if text in error),'inspect-failed')
            raise DiscoveryError(reason,completed.returncode)
        groups.append('/system.slice/docker-'+ident+'.scope')
    return groups


class DiscoveryError(RuntimeError):
    def __init__(self,reason,returncode):
        self.reason=reason;self.returncode=returncode


def protection_step(root,policy,container_groups,apply,refresh):
    """An unavailable Docker API must not skip Incus/guest protection."""
    error=None
    if refresh:
        try:
            container_groups=critical_container_groups(policy.get('oom_protection',{}).get('containers',[]))
        except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
            error={'error':type(exc).__name__}
            if isinstance(exc,DiscoveryError):
                error.update(reason=exc.reason,returncode=exc.returncode)
    changed=protect(root,policy,container_groups,apply)
    return container_groups,changed,error


def protect(root,policy,container_groups,apply):
    settings=policy.get('oom_protection',{})
    if not settings:return {}
    groups=['/lxc.payload.'+n for n in settings.get('instances',[])]
    groups+=['/system.slice/'+n for n in settings.get('units',[])]+container_groups
    # avoid is deliberately finite: critical groups remain a last-resort
    # candidate, rather than making an exhausted machine impossible to recover.
    for group in groups:
        path=root/group.lstrip('/')
        if not path.exists():continue
        if apply:
            try:
                if os.getxattr(path,'user.oomd_avoid')!=b'1':
                    os.setxattr(path,'user.oomd_avoid',b'1')
            except OSError:
                os.setxattr(path,'user.oomd_avoid',b'1')
    for name in settings.get('instances',[]):
        path=root/('lxc.payload.'+name)
        if not path.exists():continue
        limit=(path/'memory.max').read_text().strip()
        budget=settings.get('memory_low_mib',4096)*MIB
        if limit!='max':budget=min(budget,int(limit))
        if apply and int((path/'memory.low').read_text())!=budget:
            write_control(path/'memory.low',str(budget))
    changes={'critical':0,'build':0}
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():continue
        try:
            cg=(path/'cgroup').read_text().strip().split('0::',1)[1]
            comm=(path/'comm').read_text().strip()
            critical=any(in_group(cg,g) for g in groups)
            build=False
            if not critical and cg.startswith('/lxc.payload.fedora-heart/'):
                build=is_build(comm,(path/'cmdline').read_bytes() if comm=='java' else b'')
            if not critical and not build:continue
            current=int((path/'oom_score_adj').read_text())
            target=priority(current,critical,build,settings.get('critical_score_adj',-950),settings.get('build_score_adj',500))
            if current!=target:
                # Revalidate membership before writing; a disappearing process
                # must not turn into a change to an unrelated task.
                if (path/'cgroup').read_text().strip().split('0::',1)[1]!=cg:continue
                if apply:(path/'oom_score_adj').write_text(str(target))
                changes['critical' if critical else 'build']+=1
        except (OSError,ValueError,IndexError):
            continue
    return changes


def reconcile(root, name, policy, device, apply):
    path=root/('lxc.payload.'+name)
    if not path.exists():
        return {'instance':name,'state':'not-running'}
    current=io_values((path/'io.max').read_text(),device)
    updates=desired_io(current,policy['read_mib'],policy['write_mib'])
    result={'instance':name,'identity':path.stat().st_ino,'io_updates':updates}
    if updates and apply:
        write_control(path/'io.max',device+' '+ ' '.join(k+'='+v for k,v in updates.items()))
        actual=io_values((path/'io.max').read_text(),device)
        if any(actual.get(k)!=v for k,v in updates.items()):
            raise RuntimeError('bandwidth readback mismatch')
        for key in ('riops','wiops'):
            if actual.get(key,'max')!=current.get(key,'max'):
                raise RuntimeError('IOPS changed unexpectedly')
    if 'memory_high_ratio' in policy:
        expected=desired_high((path/'memory.max').read_text().strip(),policy['memory_high_ratio'])
        old=(path/'memory.high').read_text().strip()
        if old!=expected:
            result['memory_high_update']={'before':old,'after':expected}
            if apply:
                write_control(path/'memory.high',expected)
                if (path/'memory.high').read_text().strip()!=expected:
                    raise RuntimeError('memory.high readback mismatch')
    return result


def telemetry(root, device):
    guests={}
    for path in root.glob('lxc.payload.*'):
        try:
            stats=pairs((path/'memory.stat').read_text())
            guests[path.name]={
                'identity':path.stat().st_ino,
                'current':int((path/'memory.current').read_text()),
                'high':(path/'memory.high').read_text().strip(),
                'max':(path/'memory.max').read_text().strip(),
                'io':io_values((path/'io.stat').read_text(),device),
                'refault_file':stats.get('workingset_refault_file',0),
                'scan_direct':stats.get('pgscan_direct',0),
            }
        except FileNotFoundError:
            continue
    return guests


def process_io():
    result={}
    for p in Path('/proc').iterdir():
        if not p.name.isdecimal(): continue
        try:
            info=pairs((p/'io').read_text())
            cg=(p/'cgroup').read_text().strip().split('0::',1)[1]
            result[p.name]={'comm':(p/'comm').read_text().strip(), 'cgroup':cg,
                            'read':info['read_bytes:'],'write':info['write_bytes:'],
                            'rss':int((p/'statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE'),
                            'start':(p/'stat').read_text().rsplit(')',1)[1].split()[19]}
        except (OSError,ValueError,KeyError,IndexError):
            continue
    return result


def io_deltas(previous,current,seconds):
    rows=[]
    for pid,p in current.items():
        old=previous.get(pid)
        if not old or (p['comm'],p['cgroup'],p.get('start')) != (old['comm'],old['cgroup'],old.get('start')):continue
        read=p['read']-old['read'];write=p['write']-old['write']
        if min(read,write)<0:continue
        if read or write:
            rows.append({'pid':int(pid),'comm':p['comm'],'cgroup':p['cgroup'],
                         'start':p.get('start'), 'read_bps':round(read/seconds),'write_bps':round(write/seconds)})
    return sorted(rows,key=lambda x:x['write_bps']+x['read_bps'],reverse=True)[:12]


def supervise_guards(attempts, apply):
    """Only explicitly labelled Healer guards may be restarted, never workloads."""
    try:
        result=subprocess.run(['docker','ps','-q','--filter','label=io.coolify-fixkit.watchdog=true',
            '--filter','label=com.docker.compose.service=resource-guard'],capture_output=True,text=True,timeout=3,check=True)
        ids=result.stdout.split()
        if not ids:return
        if len(ids)>4:raise ValueError('watchdog inventory budget')
        fmt='[{{json .Id}},{{json .State.Pid}},{{json .State.StartedAt}}]'
        result=subprocess.run(['docker','inspect','--format',fmt,*ids],capture_output=True,text=True,timeout=3,check=True)
        import datetime
        for line in result.stdout.splitlines():
            ident,pid,started=json.loads(line)
            if not re.fullmatch(r'[a-f0-9]{64}',ident) or type(pid) is not int or pid<=0:continue
            age=time.time()-datetime.datetime.fromisoformat(started.replace('Z','+00:00')).timestamp()
            if age<120:continue
            try:
                beat=json.loads((Path('/proc')/str(pid)/'root/tmp/resource-guard-heartbeat.json').read_text())
                stale=not 0<=time.monotonic()-beat['monotonic']<=120
            except (OSError,ValueError,KeyError,TypeError):stale=True
            if not stale or time.monotonic()-attempts.get(ident,float('-inf'))<600:continue
            attempts[ident]=time.monotonic()
            print(json.dumps({'event':'guard-stale','container':ident,'apply':apply}),flush=True)
            post_event('policy-error','resource-guard','감시 루프 생존 신호가 멈췄습니다. Healer 감시기만 복구합니다.')
            if apply:
                subprocess.run(['docker','restart','--time','5',ident],capture_output=True,timeout=15,check=True)
        for ident in list(attempts):
            if ident not in ids:del attempts[ident]
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):
        print(json.dumps({'event':'guard-watchdog-error'}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy',type=Path,required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args()
    policy=validate(json.loads(args.policy.read_text()))
    device,disk=root_device()
    root=Path('/sys/fs/cgroup')
    stop=StopFlag()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,stop.set)
    old_io=process_io();old_time=time.monotonic();next_sample=old_time
    container_groups=[];next_discovery=0
    watchdog_next=0; watchdog_attempts={}
    while not stop.is_set():
        try:
            publish(METRICS, snapshot(Path("/proc"), root))
        except (OSError, ValueError, KeyError):
            print(json.dumps({"event":"metrics-publish-error"}),flush=True)
        if time.monotonic() >= watchdog_next:
            supervise_guards(watchdog_attempts, args.apply)
            watchdog_next=time.monotonic()+30
        discovery_error=None
        try:
            refresh=time.monotonic()>=next_discovery
            container_groups,changed,discovery_error=protection_step(root,policy,container_groups,args.apply,refresh)
            if refresh:next_discovery=time.monotonic()+(5 if discovery_error else 30)
            if discovery_error:
                print(json.dumps({'time':time.time(),'event':'container-discovery-error',
                    'cached_groups':len(container_groups),**discovery_error}),flush=True)
                post_event('policy-error','debian','Caddy의 Docker 정보 조회에 실패했습니다. 기존 대상과 Incus/운영 guest의 OOM 보호 적용은 계속합니다. 5초 뒤 재시도합니다.')
            if any(changed.values()):
                print(json.dumps({'time':time.time(),'event':'oom-priority','apply':args.apply,**changed}),flush=True)
        except (OSError,ValueError,RuntimeError,subprocess.TimeoutExpired) as exc:
            print(json.dumps({'time':time.time(),'event':'protection-error','error':type(exc).__name__}),flush=True)
            post_event('policy-error','debian','중요 서비스 OOM 보호 정책 적용에 실패했습니다.')
            if args.once:return 1
        for name,p in policy['instances'].items():
            try:
                result=reconcile(root,name,p,device,args.apply)
                if args.once or result.get('io_updates') or result.get('memory_high_update'):
                    print(json.dumps({'time':time.time(),'event':'host-policy','apply':args.apply,**result}),flush=True)
            except (OSError,ValueError,RuntimeError) as e:
                print(json.dumps({'time':time.time(),'event':'policy-error','instance':name,'error':type(e).__name__}),flush=True)
                post_event('policy-error',name,'메모리 또는 SSD 한도 정책 적용에 실패했습니다.')
                if args.once:return 1
        if time.monotonic()>=next_sample:
            current=process_io();now=time.monotonic()
            top=io_deltas(old_io,current,max(1,now-old_time))
            memory=sorted(({'pid':int(pid),**v} for pid,v in current.items()),key=lambda x:x['rss'],reverse=True)[:6]
            publish(Path('/run/coolify-fixkit/top-resources.json'),{'time':time.time(),'io':top,'memory':memory})
            print(json.dumps({'time':time.time(),'event':'host-io','device':device,'disk':disk,
                'guests':telemetry(root,device),
                'top_io':io_deltas(old_io,current,max(1,now-old_time)),
                'memory_pressure':Path('/proc/pressure/memory').read_text().strip(),
                'io_pressure':Path('/proc/pressure/io').read_text().strip()}),flush=True)
            old_io=current;old_time=now;next_sample=now+60
        if args.once:return 1 if discovery_error else 0
        notify_watchdog()
        stop.wait(5)
    return 0


if __name__=='__main__':raise SystemExit(main())
