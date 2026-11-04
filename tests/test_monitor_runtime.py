import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import monitor_runtime as m
import resource_guard as g
import host_guard as h
from test_resource_guard import sample

class RuntimeTests(unittest.TestCase):
    def test_signal_handler_never_acquires_an_event_mutex(self):
        code='''import threading,signal,os
from monitor_runtime import StopFlag
stop=StopFlag(); lock=threading.Event()._cond
signal.signal(signal.SIGUSR1,stop.set)
with lock: os.kill(os.getpid(),signal.SIGUSR1)
assert stop.is_set()
'''
        subprocess.run([sys.executable,'-c',code],check=True,timeout=3)

    def test_watchdog_notifies_systemd_socket(self):
        with tempfile.TemporaryDirectory() as d:
            name=str(Path(d)/'notify')
            with socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM) as sock:
                sock.bind(name);sock.settimeout(1)
                with patch.dict(os.environ,{'NOTIFY_SOCKET':name}): m.notify_watchdog()
                self.assertEqual(sock.recv(100),b'WATCHDOG=1')

    def test_cached_metrics_fresh_and_stale_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); (root/'sys/kernel/random').mkdir(parents=True)
            (root/'sys/kernel/random/boot_id').write_text('boot')
            cached=sample();cached['time']=time.time();cached['boot']='boot'
            path=root/'metrics.json';path.write_text(json.dumps(cached))
            with patch.object(g,'METRICS',path),patch.object(g,'snapshot',return_value={'fresh':True}) as read:
                self.assertEqual(g.shared_snapshot(root,root),cached);read.assert_not_called()
                cached['time']-=30;path.write_text(json.dumps(cached))
                self.assertEqual(g.shared_snapshot(root,root),{'fresh':True})
                path.write_text('{}');self.assertEqual(g.shared_snapshot(root,root),{'fresh':True})

    @patch.object(g,'docker',return_value='')
    def test_no_full_inspect_without_opted_in_exited_container(self,docker):
        self.assertEqual(g.inventory({'name':'host'}),[])
        self.assertEqual(docker.call_count,1)
        self.assertIn('label='+g.LABEL+'oom-recover=true',docker.call_args.args)

    @patch.object(h,'post_event')
    @patch.object(h.subprocess,'run')
    def test_only_opted_in_guard_is_restarted_and_cooldown_applies(self,run,post):
        ident='a'*64;started='2020-01-01T00:00:00Z'
        def command(argv,**kw):
            if argv[1]=='ps':return subprocess.CompletedProcess(argv,0,ident+'\n','')
            if argv[1]=='inspect':return subprocess.CompletedProcess(argv,0,json.dumps([ident,99999999,started]),'')
            return subprocess.CompletedProcess(argv,0,'','')
        run.side_effect=command;attempts={}
        h.supervise_guards(attempts,True);h.supervise_guards(attempts,True)
        calls=[c.args[0] for c in run.call_args_list]
        self.assertEqual([x for x in calls if x[1]=='restart'],[['docker','restart','--time','5',ident]])
        self.assertIn('label=com.docker.compose.service=resource-guard',calls[0])
        self.assertIn('label=io.coolify-fixkit.watchdog=true',calls[0])
