"""Lock-free signal handling and progress-based watchdogs. Runtime files use tmpfs."""
import json
import os
from pathlib import Path
import socket
import time

METRICS = Path('/run/coolify-fixkit/metrics.json')
HEARTBEAT = Path('/tmp/resource-guard-heartbeat.json')

class StopFlag:
    # Python signal handlers must never acquire Event/Condition locks: a signal
    # can interrupt wait() while the same thread already owns its mutex.
    def __init__(self): self.stopped = False
    def set(self, *_): self.stopped = True
    def is_set(self): return self.stopped
    def wait(self, seconds):
        deadline = time.monotonic() + seconds
        while not self.stopped and time.monotonic() < deadline:
            time.sleep(min(.2, max(0, deadline-time.monotonic())))
        return self.stopped

def publish(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value))
    temp.replace(path)

def notify_watchdog():
    address = os.getenv('NOTIFY_SOCKET')
    if not address: return
    if address.startswith('@'): address = '\0' + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.settimeout(.2)
            sock.sendto(b'WATCHDOG=1', address)
    except OSError: pass

def heartbeat():
    publish(HEARTBEAT, {'monotonic': time.monotonic()})
