"""Cheap root-SSD capacity checks; persistent threshold alerts without tree scans."""
import math
import os
import time

GIB=1024**3

def capacity_snapshot(path='/'):
    stat=os.statvfs(path)
    size=stat.f_frsize or stat.f_bsize
    total=stat.f_blocks*size
    used=max(0,stat.f_blocks-stat.f_bfree)*size
    available=max(0,stat.f_bavail)*size
    if total<=0 or used+available<=0:raise ValueError('invalid filesystem capacity')
    # Match df's user-visible percentage, including reserved blocks and rounding.
    percent=math.ceil(used*100/(used+available))
    return {'path':path,'identity':str(stat.f_fsid),'total':total,'used':used,
            'available':available,'percent':percent}


def capacity_text(disk):
    return (f"{disk['path']}: **{disk['percent']}% 사용** / 경고 기준 75%\n"
            f"사용 {disk['used']/GIB:.1f}GiB / 전체 {disk['total']/GIB:.1f}GiB\n"
            f"실제 사용 가능한 공간: {disk['available']/GIB:.1f}GiB")


class DiskCapacity:
    def __init__(self,delivery):
        self.delivery=delivery;self.next_check=0

    def update(self,now=None):
        now=time.time() if now is None else now
        if now<self.next_check:return
        self.next_check=now+60
        disk=capacity_snapshot()
        state=self.delivery.data.get('disk_capacity')
        if not state or state['identity']!=disk['identity']:
            if state:self._cancel(state)
            state={'identity':disk['identity'],'episode':f"disk:{disk['identity']}:{now}",
                   'notified_level':0,'healthy_since':None,'raised':False}
            self.delivery.data['disk_capacity']=state;self.delivery.save()
        level=next((x for x in (95,85,75) if disk['percent']>=x),0)
        if level:
            if not state['raised']:
                state['raised']=True;self.delivery.save()
            if state['healthy_since'] is not None:
                state['healthy_since']=None;self.delivery.save()
            if level<=state['notified_level']:return
            incident=state['episode']+':'+str(level)
            if any(x.get('incident')==incident for x in self.delivery.data['pending']):return
            # A newer, more severe observation replaces an undelivered warning.
            self._cancel(state)
            title='긴급 — SSD 공간 부족' if level>=95 else '확인 필요 — SSD 공간 사용률'
            body=(f"**{title}**\n{capacity_text(disk)}\n"
                  '확인할 저장소:\n- Docker 이미지·빌드 캐시: /var/lib/docker\n'
                  '- Incus 이미지·스냅샷: /var/lib/incus\n- 시스템 로그: /var/log')
            self.delivery.enqueue({'kind':'capacity','scope':'Debian SSD','message':body},incident=incident)
        elif disk['percent']<70:
            if not state['raised']:return
            if state['healthy_since'] is None:
                state['healthy_since']=now;self.delivery.save()
            elif now-state['healthy_since']>=600:
                self._cancel(state)
                state.update(episode=f"disk:{disk['identity']}:{now}",notified_level=0,healthy_since=None,raised=False)
                self.delivery.save()
        elif state['healthy_since'] is not None:
            state['healthy_since']=None;self.delivery.save()

    def _cancel(self,state):
        for level in (75,85,95):self.delivery.cancel_incident(state['episode']+':'+str(level))
