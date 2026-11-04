#!/usr/bin/env python3
"""Private local webhook + bounded, persistent Discord bot DM delivery."""
import argparse
from collections import deque
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
        self.mutex=threading.Lock();self.channel=None;self.next_try=0

    def save(self):
        tmp=self.path.with_suffix('.tmp')
        with tmp.open('w') as f:
            json.dump(self.data,f);f.flush();os.fsync(f.fileno())
        tmp.chmod(0o600);tmp.replace(self.path)

    def enqueue(self,event):
        if not isinstance(event,dict) or set(event)-{'kind','scope','message'}:raise ValueError('invalid event')
        kind=event.get('kind');scope=event.get('scope','debian');message=event.get('message','')
        if kind not in ('oom','pressure','recovered','policy-error','recovery','test'):raise ValueError('invalid kind')
        if not isinstance(scope,str) or len(scope)>120 or not isinstance(message,str) or len(message)>1200:
            raise ValueError('invalid message')
        key=kind+':'+scope
        with self.mutex:
            if kind!='test' and (time.time()-self.data['sent'].get(key,0)<300 or
                any(x['key']==key for x in self.data['pending'])):return False
            if len(self.data['pending'])>=100:raise ValueError('outbox full')
            self.data['pending'].append({'key':key,'nonce':uuid.uuid4().hex[:24],
                'text':f'[Coolify Healer] '+{'oom':'OOM 감지','pressure':'자원 압박','recovered':'압박 해소','policy-error':'보호 정책 오류','recovery':'자동 복구','test':'연결 확인'}[kind]+f'\n대상: {scope}\n{message}'.rstrip(),'created':time.time()})
            self.save();return True

    def deliver(self):
        with self.mutex:
            if not self.data['pending'] or time.monotonic()<self.next_try:return False
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
            self.save()
        print(json.dumps({'event':'discord-delivered','kind':item['key'].split(':')[0],'message_id':response['id']}),flush=True)
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
    previous=None;bad=good=0;active=delivery.data.get('pressure_active',False)
    while not stop.is_set():
        try:
            sample=metrics.shared_snapshot(Path('/proc'),Path('/sys/fs/cgroup'))
            events=metrics.changes(previous,sample)
            if events:
                total=sum(x['oom_kill_delta'] for x in events if x['scope']=='host-inclusive')
                scopes=[x['scope'] for x in events if x['scope']!='host-inclusive']
                delivery.enqueue({'kind':'oom','scope':'debian','message':f'OOM 종료 {total}건 감지. 하위 대상: '+(', '.join(scopes) or '호스트 컨테이너/서비스')})
            reasons=metrics.reasons(sample)
            for name,guest in sample['guests'].items():
                reasons.extend(name+':'+r for r in metrics.reasons(sample,guest=guest) if r in ('guest-memory-pressure','guest-io-stall'))
            bad=bad+1 if reasons else 0;good=good+1 if not reasons else 0
            if bad>=3 and not active:
                host=sample['host']
                context={'event':'resource-pressure','time':sample['time'],'reasons':reasons,
                    'available_mib':round(host['available']/metrics.MIB),
                    'memory_some_avg10':host['memory_psi']['some']['avg10'],
                    'io_full_avg10':host['io_psi']['full']['avg10']}
                print(json.dumps(context),flush=True)
                detail=f"여유 RAM {context['available_mib']}MiB, 메모리 대기 {context['memory_some_avg10']:.2f}%, I/O 전체 대기 {context['io_full_avg10']:.2f}%. "
                delivery.enqueue({'kind':'pressure','scope':'debian','message':('15초 이상 자원 압박 지속. '+detail+'원인: '+', '.join(reasons))[:1200]})
                active=True
                with delivery.mutex:
                    delivery.data['pressure_active']=True;delivery.save()
            if active and good>=3:
                print(json.dumps({'event':'resource-recovered','time':sample['time']}),flush=True)
                delivery.enqueue({'kind':'recovered','scope':'debian','message':'메모리 및 I/O 압박이 해소됐습니다. 애플리케이션 상태는 별도 확인이 필요합니다.'})
                active=False
                with delivery.mutex:
                    delivery.data['pressure_active']=False;delivery.save()
            previous=sample
        except (OSError,ValueError,KeyError):
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
