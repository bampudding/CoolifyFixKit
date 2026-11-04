#!/usr/bin/env python3
"""Private local webhook + bounded, persistent Discord bot DM delivery."""
import argparse
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socketserver
import threading
import time
import urllib.error
import urllib.request
import uuid
import resource_guard as metrics
from monitor_runtime import notify_watchdog
from alert_policy import Incidents, stamp


def request(token,path,data):
    req=urllib.request.Request('https://discord.com/api/v10'+path,
        data=json.dumps(data).encode(),headers={'Authorization':'Bot '+token,
        'Content-Type':'application/json','User-Agent':'CoolifyFixKit (https://github.com/bampudding/CoolifyFixKit, 1.0)'})
    try:
        with urllib.request.urlopen(req,timeout=8) as response:return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code==429:
            try: delay=float(json.loads(error.read(8192)).get('retry_after',30))
            except (ValueError,TypeError):delay=30
            raise Retry(max(1,min(delay,3600))) from None
        raise Retry(300 if error.code in (401,403) else 30) from None
    except (OSError,ValueError):raise Retry(30) from None


class Retry(Exception):
    def __init__(self,delay):self.delay=delay


class Delivery:
    def __init__(self,credentials,state):
        self.credentials=credentials;self.path=state/'discord-outbox.json'
        state.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock=(state/'discord-notify.lock').open('a')
        fcntl.flock(self.lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.data=json.loads(self.path.read_text()) if self.path.exists() else {'pending':[],'sent':{}}
        self.mutex=threading.RLock();self.channel=None;self.next_try=0
        old=self.data['pending']
        self.data['pending']=[x for x in old if x['key'].split(':')[0]!='recovered' and
            not (x['key'].split(':')[0]=='pressure' and not x.get('incident'))]
        if len(old)!=len(self.data['pending']):self.save()

    def save(self):
        tmp=self.path.with_suffix('.tmp')
        with tmp.open('w') as f:
            json.dump(self.data,f);f.flush();os.fsync(f.fileno())
        tmp.chmod(0o600);tmp.replace(self.path)

    def enqueue(self,event, *, incident=None):
        if not isinstance(event,dict) or set(event)-{'kind','scope','message'}:raise ValueError('invalid event')
        kind=event.get('kind');scope=event.get('scope','debian');message=event.get('message','')
        if kind not in ('oom','pressure','recovered','policy-error','recovery','weekly','test'):raise ValueError('invalid kind')
        if not isinstance(scope,str) or len(scope)>120 or not isinstance(message,str) or len(message)>1200:
            raise ValueError('invalid message')
        key=kind+':'+scope
        if incident:key+=':'+incident
        elif kind=='policy-error':key+=':'+hashlib.sha256(message.encode()).hexdigest()[:12]
        with self.mutex:
            if kind=='recovered':return False
            if kind=='weekly' and key in self.data.get('weekly_sent',{}):return False
            if kind!='test' and (time.time()-self.data['sent'].get(key,0)<(1800 if kind=='policy-error' else 300) or
                any(x['key']==key for x in self.data['pending'])):return False
            if len(self.data['pending'])>=100:raise ValueError('outbox full')
            titles={'oom':'OOM 종료 감지','pressure':'자원 상태','recovered':'자원 상태 해소','policy-error':'보호 기능 점검 필요','recovery':'컨테이너 복구 요청','weekly':'Now Brief','test':'알림 연결 확인'}
            scope_name={'memory':'메모리','io':'디스크 I/O'}.get(scope,scope)
            self.data['pending'].append({'key':key,'kind':kind,'incident':incident,'nonce':uuid.uuid4().hex[:24],
                'text':f"**Coolify Healer | {titles[kind]}**\n대상: {scope_name} / {stamp(time.time())}\n{message}".rstrip(),
                'created':time.time()})
            self.save();return True

    def cancel_incident(self,incident):
        with self.mutex:
            self.data['pending']=[x for x in self.data['pending'] if x.get('incident')!=incident]
            self.save()

    def deliver(self):
        with self.mutex:
            if not self.data['pending'] or time.monotonic()<self.next_try:return False
            while self.data['pending']:
                candidate=self.data['pending'][0]
                kind=candidate.get('kind',candidate['key'].split(':')[0])
                ttl=900 if kind in ('pressure','recovered') else (604800 if kind=='weekly' else 86400)
                if time.time()-candidate['created']<=ttl:break
                self.data['pending'].pop(0);self.save()
            if not self.data['pending']:return False
            item=dict(self.data['pending'][0])
        config=json.loads(self.credentials.read_text())
        try:
            if not self.channel:
                self.channel=request(config['bot_token'],'/users/@me/channels',{'recipient_id':config['recipient_id']})['id']
            response=request(config['bot_token'],'/channels/'+self.channel+'/messages',{
                'content':item['text'],'allowed_mentions':{'parse':[]},'nonce':item['nonce'],'enforce_nonce':True})
        except Retry as error:
            self.next_try=time.monotonic()+error.delay
            print(json.dumps({'event':'discord-retry','retry_seconds':error.delay}),flush=True);return False
        with self.mutex:
            self.data['pending'].pop(0);self.data['sent'][item['key']]=time.time()
            self.data['sent']={k:v for k,v in self.data['sent'].items() if time.time()-v<86400}
            if item.get('kind')=='weekly':
                self.data.setdefault('weekly_sent',{})[item['key']]=time.time()
            self.save()
        print(json.dumps({'event':'discord-delivered','kind':item['key'].split(':')[0],'policy_version':2,'message_id':response['id']}),flush=True)
        return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def do_POST(self):
        self.connection.settimeout(3)
        if self.path!='/events':self.send_error(404);return
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=4096:raise ValueError('size')
            accepted=self.server.delivery.enqueue(json.loads(self.rfile.read(size)))
        except (ValueError,OSError):self.send_error(400);return
        self.send_response(202 if accepted else 200);self.end_headers();self.wfile.write(b'{}')


class Server(socketserver.UnixStreamServer):
    allow_reuse_address=True


def watch(delivery,stop):
    previous=None;policy=Incidents(delivery)
    while not stop.is_set():
        try:
            sample=metrics.shared_snapshot(Path('/proc'),Path('/sys/fs/cgroup'))
            baseline=previous or delivery.data.get('oom_baseline')
            events=metrics.changes(baseline,sample)
            if events:
                total=sum(x['oom_kill_delta'] for x in events if x['scope']=='host-inclusive')
                scopes=[x['scope'].removeprefix('lxc.payload.') for x in events if x['scope']!='host-inclusive']
                body=f"**긴급 — OOM 종료 {total}건**\n관측: {stamp(sample['time'])}\n대상: "+(', '.join(scopes) or '호스트 컨테이너/서비스')+"\n확인: 종료된 서비스와 최근 빌드의 메모리를 점검하세요. 이 알림은 복구 성공을 의미하지 않습니다."
                from weekly_brief import oom_context
                context=oom_context(max(120,min(604800,sample['time']-delivery.data.get('oom_baseline',{}).get('observed_at',sample['time']-120))),limit=max(1,min(total,4)))
                if context:body+='\n확인된 종료 기록:\n'+context
                delivery.enqueue({'kind':'oom','scope':'debian','message':body[:1200]},incident=f"{sample['boot']}:{sample['host']['oom_kill']}")
            # Persist only small counter baselines, not full telemetry every tick.
            baseline={'boot':sample['boot'],'host':{'oom_kill':sample['host']['oom_kill']},
                'guests':{n:{'identity':v['identity'],'events':{'oom_kill':v['events']['oom_kill']}} for n,v in sample['guests'].items()}}
            if {k:v for k,v in delivery.data.get('oom_baseline',{}).items() if k!='observed_at'}!=baseline:
                baseline['observed_at']=sample['time']
                with delivery.mutex:
                    delivery.data['oom_baseline']=baseline;delivery.save()
            with delivery.mutex:policy.update(sample)
            previous=sample
        except (OSError,ValueError,KeyError,TypeError):
            print(json.dumps({'event':'discord-monitor-error'}),flush=True)
        try:delivery.deliver()
        except (OSError,ValueError,KeyError):print(json.dumps({'event':'discord-delivery-config-error'}),flush=True)
        notify_watchdog()
        stop.wait(5)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credentials',type=Path,default=Path('/etc/coolify-fixkit/discord.json'))
    parser.add_argument('--state',type=Path,default=Path('/var/lib/coolify-fixkit'))
    parser.add_argument('--socket',type=Path,default=Path('/run/coolify-fixkit/notify.sock'))
    parser.add_argument('--test',action='store_true');args=parser.parse_args()
    os.umask(0o077)
    if args.test:
        # Use the running webhook so the single persistent outbox owns delivery.
        import http.client,socket
        conn=http.client.HTTPConnection('localhost',timeout=5)
        conn.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);conn.sock.settimeout(5);conn.sock.connect(str(args.socket))
        conn.request('POST','/events',json.dumps({'kind':'test','scope':'debian','message':'개인 메시지 알림 연결을 확인했습니다. OOM, 지속되는 자원 압박과 압박 해소를 이 봇으로 알려드립니다.'}),{'Content-Type':'application/json'})
        response=conn.getresponse();print('test queued',response.status);return 0 if response.status==202 else 1
    delivery=Delivery(args.credentials,args.state);args.socket.parent.mkdir(parents=True,exist_ok=True)
    args.socket.unlink(missing_ok=True)
    with Server(str(args.socket),Handler) as server:
        args.socket.chmod(0o600);server.delivery=delivery
        stop=threading.Event();threading.Thread(target=watch,args=(delivery,stop),daemon=True).start()
        try:server.serve_forever(poll_interval=.5)
        finally:stop.set();args.socket.unlink(missing_ok=True)
    return 0


if __name__=='__main__':raise SystemExit(main())
