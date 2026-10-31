#!/usr/bin/env python3
"""Explicit, disposable 64MiB cgroup OOM test. Run only on the audited Debian host."""
import argparse
import json
from pathlib import Path
import tempfile
import time
import uuid
import resource_guard as g

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--run', action='store_true', required=True)
parser.parse_args()
scope = {'name': 'host', 'cgroup': None}
key = 'oom-audit-' + uuid.uuid4().hex[:12]
container = None
program = "from pathlib import Path; import time; p=Path('/tmp/first-attempt'); first=not p.exists(); p.touch(); a=bytearray(128*1024*1024) if first else bytearray(1024); time.sleep(120)"
try:
    container = g.run(['docker', 'create', '--name', key, '--network', 'none', '--memory', '64m',
        '--memory-swap', '64m', '--cpus', '0.25', '--pids-limit', '16', '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges', '--label', g.LABEL+'oom-recover=true',
        '--label', g.LABEL+'recovery-key='+key, '--label', g.LABEL+'restart-memory-mib=64',
        'coolify-fixkit:audit-20261031', 'python3', '-c', program]).strip()
    assert len(container) == 64
    g.run(['docker', 'start', container])
    assert g.run(['docker', 'wait', container]).strip() == '137'
    real_inventory = g.inventory
    def only_test(target):
        return [i for i in real_inventory(target) if i['id'] == container]
    g.inventory = only_test
    assert g.candidate(only_test(scope)[0], only_test(scope)) is not None
    with tempfile.TemporaryDirectory(prefix='fixkit-recovery-audit-') as directory:
        store = g.Store(Path(directory))
        try:
            for attempt in range(20):
                current = g.snapshot(Path('/proc'), Path('/sys/fs/cgroup'))
                if not g.reasons(current, required=64*g.MIB):
                    break
                time.sleep(1)
            else:
                raise RuntimeError('host pressure still high; recovery correctly deferred')
            g.recover(scope, current, store, True, Path('/proc'), Path('/sys/fs/cgroup'), 4*g.GIB)
            assert only_test(scope)[0]['state']['Running']
            g.recover(scope, current, store, True, Path('/proc'), Path('/sys/fs/cgroup'), 4*g.GIB)
            assert len(store.data['host:'+key]) == 1
            g.run(['docker', 'exec', container, 'test', '-f', '/tmp/first-attempt'])
            print(json.dumps({'event': 'integration-recovered', 'container': container,
                              'memory_limit_mib': 64, 'attempts': 1, 'duplicate_start': False}))
        finally:
            store.lock.close()
finally:
    if container:
        g.run(['docker', 'rm', '-f', container])
