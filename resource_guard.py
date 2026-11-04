#!/usr/bin/env python3
"""Read host/cgroup v2 pressure and recover explicitly opted-in OOM exits.

No workload is stopped, no limit is raised, and no Incus instance is restarted.
The default is observation. All commands use argv and bounded timeouts.
"""

import argparse
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from event_webhook import post_event
from monitor_runtime import StopFlag, METRICS, heartbeat

MIB = 1024**2
GIB = 1024**3
LABEL = "io.coolify-fixkit."


def emit(event, **fields):
    print(json.dumps({"time": time.time(), "event": event, **fields}), flush=True)
    if event in ("recovery-start-requested", "recovery-error"):
        post_event("recovery", fields.get("scope", "debian"),
                   "컨테이너 시작 요청 완료. 건강 상태는 별도 확인이 필요합니다." if event == "recovery-start-requested" else "자동 복구 요청에 실패했습니다.")


def keyed(text):
    return {k.rstrip(":"): int(v.split()[0]) for k, v in
            (line.split(None, 1) for line in text.splitlines() if line.strip())}


def pressure(text):
    result = {}
    for line in text.splitlines():
        kind, *values = line.split()
        result[kind] = {k: float(v) for k, v in (x.split("=") for x in values)}
    if "some" not in result or "full" not in result:
        raise ValueError("missing PSI some/full")
    if any(not math.isfinite(v) or v < 0 for row in result.values() for v in row.values()):
        raise ValueError("invalid PSI value")
    return result


def read_limit(path):
    value = path.read_text().strip()
    return None if value == "max" else int(value)


def cgroup_snapshot(path):
    st = path.stat()
    stats = keyed((path / "memory.stat").read_text())
    return {
        "identity": [st.st_dev, st.st_ino],
        "current": int((path / "memory.current").read_text()),
        "high": read_limit(path / "memory.high"),
        "max": read_limit(path / "memory.max"),
        "swap": int((path / "memory.swap.current").read_text()),
        "swap_max": read_limit(path / "memory.swap.max"),
        "events": keyed((path / "memory.events").read_text()),
        "memory_psi": pressure((path / "memory.pressure").read_text()),
        "io_psi": pressure((path / "io.pressure").read_text()),
        "stats": {k: stats.get(k, 0) for k in (
            "anon", "file", "shmem", "file_dirty", "file_writeback",
            "workingset_refault_file", "pgscan_direct", "pgmajfault")},
    }


def snapshot(proc, cgroups):
    memory = keyed((proc / "meminfo").read_text())
    total, available = memory["MemTotal"] * 1024, memory["MemAvailable"] * 1024
    if not 0 <= available <= total or total <= 0:
        raise ValueError("invalid host memory")
    host = {
        "total": total, "available": available,
        "swap_used": (memory["SwapTotal"] - memory["SwapFree"]) * 1024,
        "memory_psi": pressure((proc / "pressure/memory").read_text()),
        "io_psi": pressure((proc / "pressure/io").read_text()),
        "oom_kill": keyed((proc / "vmstat").read_text())["oom_kill"],
    }
    # Read only Incus roots, including every project; counters are hierarchical,
    # so a child service OOM is visible here even when the guest itself survives.
    guests, errors = {}, []
    if not (cgroups / "cgroup.controllers").is_file():
        raise ValueError("host cgroup v2 mount missing")
    for path in sorted(cgroups.glob("lxc.payload.*")):
        try:
            guests[path.name] = cgroup_snapshot(path)
        except (OSError, ValueError) as exc:
            errors.append({"cgroup": path.name, "error": type(exc).__name__})
    return {"time": time.time(), "boot": (proc / "sys/kernel/random/boot_id").read_text().strip(),
            "host": host, "guests": guests, "errors": errors}


def shared_snapshot(proc, cgroups):
    """Use the host's fresh tmpfs sample; fail back to direct reads on any issue."""
    try:
        sample = json.loads(METRICS.read_text())
        if (0 <= time.time()-sample['time'] <= 15 and
                sample['boot'] == (proc/'sys/kernel/random/boot_id').read_text().strip()):
            # Validate nested data before accepting a cached observation.
            reasons(sample)
            for guest in sample['guests'].values(): reasons(sample, guest=guest)
            return sample
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return snapshot(proc, cgroups)


def reasons(sample, reserve=4 * GIB, required=0, guest=None):
    host = sample["host"]
    result = []
    if host["available"] < reserve + required:
        result.append("host-memory-reserve")
    for owner, data in [("host", host), *([] if guest is None else [("guest", guest)])]:
        if data["memory_psi"]["some"]["avg10"] >= 10:
            result.append(owner + "-memory-pressure")
        if data["io_psi"]["full"]["avg10"] >= 10:
            result.append(owner + "-io-stall")
    if sample["errors"]:
        result.append("incomplete-telemetry")
    if guest is not None:
        # Respect the effective soft ceiling too: don't restart straight into
        # the reclaim/refault storm that this guard is meant to diagnose.
        # Apply the ratio once to the hard allocation. memory.high is already
        # a soft ceiling, so multiplying it again would restrict a 93% policy
        # to 83.7%. Respect an independently stricter soft ceiling as-is.
        caps = ([guest["max"] * 0.93] if guest["max"] is not None else [])
        if guest["high"] is not None:
            caps.append(guest["high"])
        if caps and guest["current"] + required > min(caps):
            result.append("guest-memory-headroom")
    return result


def changes(previous, current):
    events = []
    if previous is None or previous["boot"] != current["boot"]:
        return events
    delta = current["host"]["oom_kill"] - previous["host"]["oom_kill"]
    if delta > 0:
        events.append({"scope": "host-inclusive", "oom_kill_delta": delta})
    for name, guest in current["guests"].items():
        old = previous["guests"].get(name)
        if not old or old["identity"] != guest["identity"]:
            continue
        kills = guest["events"]["oom_kill"] - old["events"]["oom_kill"]
        if kills > 0:
            events.append({"scope": name, "oom_kill_delta": kills})
    return events


def forecasts(previous, current):
    """Short-window linear estimate, reported as a warning, never a kill signal."""
    if not previous or previous["boot"] != current["boot"]:
        return []
    elapsed = current["time"] - previous["time"]
    if elapsed <= 0 or elapsed > 120:
        return []
    result = []
    for name, guest in current["guests"].items():
        old = previous["guests"].get(name)
        if not old or old["identity"] != guest["identity"] or not guest["max"]:
            continue
        growth = (guest["current"] - old["current"]) / elapsed
        if growth > 16 * MIB:
            seconds = (guest["max"] - guest["current"]) / growth
            if 0 <= seconds < 120:
                result.append({"scope": name, "seconds_to_max_estimate": round(seconds, 1)})
    return result


def run(argv, timeout=12):
    completed = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=timeout, check=False)
    if completed.returncode:
        # stderr can include credentials/URLs; keep it out of persistent logs.
        raise RuntimeError("command failed: " + argv[0])
    return completed.stdout


def docker(scope, *args):
    prefix = [] if scope["name"] == "host" else [
        "incus", "--force-local", "--project", scope["project"], "exec", scope["instance"],
        "--disable-stdin", "--mode=non-interactive", "--"]
    return run([*prefix, "docker", *args])


def guest_cgroup(text):
    unified = next((line[3:] for line in text.splitlines() if line.startswith("0::")), None)
    if unified is None:
        raise ValueError("missing unified cgroup")
    # With Docker's private cgroup namespace, a host /proc bind reports peers
    # as /../../lxc.payload.NAME/init.scope. Select the kernel's Incus component
    # rather than joining that relative path onto the read-only host mount.
    roots = [part for part in unified.split("/") if part.startswith("lxc.payload.")]
    if len(roots) != 1:
        raise ValueError("unexpected Incus cgroup")
    return roots[0]


def scopes(incus, proc):
    result = [{"name": "host", "cgroup": None}]
    if incus:
        instances = json.loads(run(["incus", "--force-local", "list", "--all-projects", "--format=json"]))
        for item in instances:
            if item["status"] != "Running" or item["type"] != "container":
                continue
            # Derive the real cgroup from the init PID; don't guess project names.
            pid = int(item["state"]["pid"])
            root = guest_cgroup((proc / str(pid) / "cgroup").read_text())
            result.append({"name": item["project"] + "/" + item["name"], "project": item["project"],
                           "instance": item["name"], "cgroup": root})
    return result


def inventory(scope):
    # Include replacements that have opted OUT as well; an old deployment must
    # never be resurrected just because its replacement dropped the opt-in label.
    if not docker(scope, "ps", "-aq", "--filter", "label=" + LABEL + "oom-recover=true",
                  "--filter", "status=exited").split():
        return []
    ids = docker(scope, "ps", "-aq").split()
    if not ids:
        return []
    if len(ids) > 1000:
        raise ValueError("inventory exceeds recovery scan budget")
    # Select only state and the three recovery labels; don't capture app env.
    # JSON array formatting avoids shell parsing of arbitrary labels.
    fmt = '[{{json .Id}},{{json .State}},{{json .Created}},{{json .Config.Labels}}]'
    result = []
    for offset in range(0, len(ids), 50):
        for line in docker(scope, "inspect", "--format", fmt, *ids[offset:offset + 50]).splitlines():
            ident, state, created, labels = json.loads(line)
            result.append({"id": ident, "state": state, "created": created,
                           "labels": {k: v for k, v in (labels or {}).items() if k.startswith(LABEL) or
                                      k in ("coolify.applicationId", "com.docker.compose.project",
                                            "com.docker.compose.service")}})
    return result


def candidate(item, items):
    labels, state = item["labels"], item["state"]
    key = labels.get(LABEL + "recovery-key", "")
    budget = labels.get(LABEL + "restart-memory-mib", "")
    if labels.get(LABEL + "oom-recover") != "true" or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", key):
        return None
    if not budget.isdecimal() or not 64 <= int(budget) <= 1048576:
        return None
    if not state.get("OOMKilled") or state.get("Status") != "exited" or state.get("ExitCode") != 137:
        return None
    if not state.get("FinishedAt") or state.get("Running") or state.get("Restarting"):
        return None
    try:
        finished = dt.datetime.fromisoformat(state["FinishedAt"].replace("Z", "+00:00"))
        if finished.tzinfo is None or not 0 <= time.time() - finished.timestamp() < 86400:
            return None
    except ValueError:
        return None
    for other in items:
        other_labels = other["labels"]
        same_owner = any(labels.get(owner) and labels.get("com.docker.compose.service") and
                         labels.get(owner) == other_labels.get(owner) and
                         labels.get("com.docker.compose.service") == other_labels.get("com.docker.compose.service")
                         for owner in ("coolify.applicationId", "com.docker.compose.project"))
        if other["id"] != item["id"] and (other_labels.get(LABEL + "recovery-key") == key or same_owner):
            if other["state"].get("Running") or other["created"] >= item["created"]:
                return None
    return key, int(budget) * MIB


class Store:
    def __init__(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "recovery.json"
        # One writer per state volume; fail rather than running a second healer.
        self.lock = (directory / "guard.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.data = json.loads(self.path.read_text()) if self.path.exists() else {}
            if not isinstance(self.data, dict) or any(
                    not isinstance(entries, list) or any(
                        not isinstance(entry, dict) or not isinstance(entry.get("incident"), str) or
                        type(entry.get("time")) not in (int, float) or not math.isfinite(entry["time"])
                        for entry in entries) for entries in self.data.values()):
                raise ValueError("invalid recovery state")
        except BaseException:
            self.lock.close()
            raise

    def save(self):
        temp = self.path.with_suffix(".tmp")
        with temp.open("w") as stream:
            json.dump(self.data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(self.path)
        fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def reserve(self, key, incident, now):
        attempts = self.data.setdefault(key, [])
        attempts[:] = [x for x in attempts if now - x["time"] < 86400]
        if any(x["incident"] == incident or now - x["time"] < 3600 for x in attempts):
            return False
        if len(attempts) >= 3:
            return False
        global_recent = sum(now - x["time"] < 3600 for a in self.data.values() for x in a)
        if global_recent >= 3:
            return False
        attempts.append({"incident": incident, "time": now})
        self.save()  # Persist before action, including failures and timeouts.
        return True


def recover(scope, sample, store, apply, proc, cgroups, reserve):
    items = inventory(scope)
    for item in items:
        choice = candidate(item, items)
        if choice is None:
            continue
        key, required = choice
        guest = sample["guests"].get(scope["cgroup"]) if scope["cgroup"] else None
        blocked = reasons(sample, reserve, required, guest)
        if scope["cgroup"] and guest is None:
            blocked.append("guest-telemetry-missing")
        if blocked:
            emit("recovery-deferred", scope=scope["name"], key=key, reasons=blocked)
            continue
        if not apply:
            emit("recovery-proposed", scope=scope["name"], key=key, container=item["id"])
            continue
        # Re-read state, deployment generation and pressure immediately before action.
        fresh = inventory(scope)
        target = next((x for x in fresh if x["id"] == item["id"]), None)
        if target is None or candidate(target, fresh) != choice or target["state"] != item["state"]:
            continue
        latest = snapshot(proc, cgroups)
        latest_guest = latest["guests"].get(scope["cgroup"]) if scope["cgroup"] else None
        if scope["cgroup"] and (latest_guest is None or latest_guest["identity"] != guest["identity"]):
            continue
        if reasons(latest, reserve, required, latest_guest):
            continue
        incident = item["id"] + ":" + item["state"]["FinishedAt"]
        if not store.reserve(scope["name"] + ":" + key, incident, time.time()):
            continue
        emit("recovery-start", scope=scope["name"], key=key, container=item["id"])
        docker(scope, "start", item["id"])
        # Starting is distinct from application readiness, which is observed by
        # Docker/Coolify healthchecks; do not claim the application is healthy.
        emit("recovery-start-requested", scope=scope["name"], key=key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("watch", "once", "check"), nargs="?", default="watch")
    parser.add_argument("--proc", type=Path, default=Path(os.getenv("HOST_PROC", "/host/proc")))
    parser.add_argument("--cgroups", type=Path, default=Path(os.getenv("HOST_CGROUP", "/host/cgroup")))
    parser.add_argument("--state", type=Path, default=Path(os.getenv("GUARD_STATE", "/state")))
    parser.add_argument("--required-mib", type=int, default=0)
    args = parser.parse_args()
    reserve = int(os.getenv("HOST_RESERVE_MIB", "4096")) * MIB
    interval = int(os.getenv("GUARD_INTERVAL", "5"))
    log_interval = int(os.getenv("GUARD_LOG_INTERVAL", "60"))
    if reserve < 256 * MIB or interval < 1 or log_interval < interval or args.required_mib < 0:
        parser.error("invalid reserve, interval or workload budget")
    if args.mode == "check":
        try:
            sample = snapshot(args.proc, args.cgroups)
            blocked = reasons(sample, reserve, args.required_mib * MIB)
            for name, guest in sample["guests"].items():
                blocked.extend(name + ":" + reason for reason in reasons(sample, reserve, guest=guest)
                               if reason.startswith("guest-"))
            emit("admission", allowed=not blocked, reasons=blocked)
            return int(bool(blocked))
        except (OSError, ValueError, KeyError):
            emit("admission", allowed=False, reasons=["telemetry-unavailable"])
            return 1
    store = Store(args.state)
    stop = StopFlag()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop.set)
    previous, healthy, tick = None, 0, 0
    last_log, last_warnings = float("-inf"), None
    apply = os.getenv("GUARD_APPLY", "false") == "true"
    while not stop.is_set():
        try:
            sample = shared_snapshot(args.proc, args.cgroups)
            blocked = reasons(sample, reserve)
            guest_warnings = {name: reasons(sample, reserve, guest=guest)
                              for name, guest in sample["guests"].items()}
            oom_events = changes(previous, sample)
            predictions = forecasts(previous, sample)
            warnings = json.dumps([blocked, guest_warnings], sort_keys=True)
            if time.monotonic() - last_log >= log_interval or warnings != last_warnings or oom_events or predictions:
                emit("sample", **sample, admission_blocked=blocked,
                     guest_warnings=guest_warnings, oom_events=oom_events, forecasts=predictions)
                last_log, last_warnings = time.monotonic(), warnings
            healthy = healthy + 1 if not blocked else 0
            if healthy >= 3 and tick % 6 == 0:
                for scope in scopes(os.getenv("INCUS_ENABLED", "false") == "true", args.proc):
                    try:
                        recover(scope, sample, store, apply, args.proc, args.cgroups, reserve)
                    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        emit("recovery-error", scope=scope["name"], error=type(exc).__name__)
            previous = sample
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
            healthy = 0
            emit("telemetry-error", error=type(exc).__name__)
            if args.mode == "once":
                return 1
        if args.mode == "once":
            return 0
        heartbeat()
        tick += 1
        stop.wait(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
