#!/usr/bin/env python3
"""Bounded, read-only 4KiB QD1 latency sample; never a write/endurance test."""
import argparse
import json
import mmap
import os
import random
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('device')
args = parser.parse_args()
fd = os.open(args.device, os.O_RDONLY | os.O_DIRECT)
try:
    size = os.lseek(fd, 0, os.SEEK_END)
    block = mmap.mmap(-1, 4096)
    rng = random.Random(20261031)
    latencies = []
    for _ in range(400):
        offset = rng.randrange(256, size//4096 - 256)*4096
        start = time.monotonic()
        if os.preadv(fd, [block], offset) != 4096:
            raise OSError('short direct read')
        elapsed = time.monotonic() - start
        latencies.append(elapsed*1000)
        time.sleep(max(0, 0.01-elapsed))
    latencies.sort()
    print(json.dumps({'device': args.device, 'method': 'O_RDONLY|O_DIRECT, 4KiB QD1, at most 100 IOPS',
                      'samples': len(latencies), 'bytes_read': len(latencies)*4096,
                      'p50_ms': latencies[199], 'p95_ms': latencies[379],
                      'p99_ms': latencies[395], 'max_ms': latencies[-1]}))
    block.close()
finally:
    os.close(fd)
