#!/usr/bin/env python3
"""Summarize sadf -j files with explicit UTC -> Asia/Seoul conversion."""
import argparse
import csv
import datetime as dt
import json
from pathlib import Path
import statistics
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('files', nargs='+', type=Path)
args = parser.parse_args()
rows = []
for path in sorted(args.files):
    samples = json.loads(path.read_text())['sysstat']['hosts'][0]['statistics']
    if not samples:
        continue
    entries = []
    for sample in samples:
        t = sample['timestamp']
        if t['tz'] != 'UTC':
            raise ValueError('expected sadf UTC timestamps')
        local = dt.datetime.fromisoformat(t['date']+'T'+t['time']).replace(tzinfo=dt.timezone.utc).astimezone(dt.timezone(dt.timedelta(hours=9)))
        disk = next(d for d in sample['disk'] if d['disk-device'] == 'sda')
        entries.append({'time': local.isoformat(), 'seconds': t['interval'], 'avail': sample['memory']['avail']/1048576,
                        'swap': sample['memory']['swpused']/1048576,
                        'iowait': sample['cpu-load'][0]['iowait'], 'load': sample['queue']['ldavg-1'],
                        'util': disk['util-percent'], 'await': disk['await'],
                        'read': disk['rkB']/1024, 'write': disk['wkB']/1024})
    rows.append({'date_kst': entries[0]['time'][:10], 'samples': len(entries),
                 'available_min_gib': round(min(e['avail'] for e in entries),2),
                 'available_median_gib': round(statistics.median(e['avail'] for e in entries),2),
                 'swap_max_gib': round(max(e['swap'] for e in entries),2),
                 'iowait_max_percent': max(e['iowait'] for e in entries),
                 'load1_max': max(e['load'] for e in entries),
                 'ssd_util_max_percent': max(e['util'] for e in entries),
                 'ssd_await_max_ms': max(e['await'] for e in entries),
                 'ssd_read_max_mib_s': round(max(e['read'] for e in entries),1),
                 'ssd_write_max_mib_s': round(max(e['write'] for e in entries),1),
                 'ssd_read_gib': round(sum(e['read']*e['seconds'] for e in entries)/1024,1),
                 'ssd_write_gib': round(sum(e['write']*e['seconds'] for e in entries)/1024,1)})
writer = csv.DictWriter(sys.stdout, fieldnames=list(rows[0]), lineterminator='\n')
writer.writeheader()
writer.writerows(rows)
