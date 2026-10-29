"""Background sampler: measures the machine, lowers agent priority, rescues low memory."""
import collections
import os
import signal
import sys
import threading
import time

from .common import PROTECTED, renice

INTERVAL = 2.0
RESCUE_COOLDOWN = 15.0
RESCUE_MIN_RSS = 200 * 2 ** 20  # don't bother stopping anything smaller


def guard_targets(procs, conf, uid, self_pid):
    """Own processes that are build tools, or are (started by) an AI agent."""
    roots, names = set(conf["guard_roots"]), set(conf["guard_names"])
    memo = {}

    def under_root(pid):
        chain = []
        while pid in procs and pid not in memo and len(chain) < 64:
            chain.append(pid)
            if procs[pid]["comm"] in roots:
                break
            pid = procs[pid]["ppid"]
        hit = memo.get(pid, pid in procs and procs[pid]["comm"] in roots)
        for p in chain:
            memo[p] = hit
        return hit

    return {pid for pid, p in procs.items()
            if p["uid"] == uid and pid != self_pid and (p["comm"] in names or under_root(pid))}


def memory_low(mem, pressure, min_available_pct):
    if pressure.get("memory_level") == "critical":
        return True
    return bool(mem["total"]) and mem["available"] < mem["total"] * min_available_pct / 100


def rescue_target(procs, targets, conf):
    """The biggest build an agent started. Never the agent itself: that would lose its session."""
    roots = set(conf["guard_roots"])
    candidates = [procs[pid] for pid in targets
                  if procs[pid]["comm"] not in roots and procs[pid]["comm"] not in PROTECTED
                  and procs[pid]["rss"] >= RESCUE_MIN_RSS]
    return max(candidates, key=lambda p: p["rss"], default=None)


class Monitor(threading.Thread):
    def __init__(self, platform, get_conf):
        super().__init__(daemon=True)
        self.platform = platform
        self.get_conf = get_conf
        self.uid = os.getuid()
        self.self_pid = os.getpid()
        self.ncpu = os.cpu_count() or 1
        self.lock = threading.Lock()
        self.history = collections.deque(maxlen=90)
        self.events = collections.deque(maxlen=10)
        self.snapshot = {}
        self.reniced = 0
        self.last_rescue = 0.0
        self.cpu_times = {p["key"]: p["cpu_time"] for p in platform.list_procs().values()}
        self.t = time.monotonic()

    def run(self):
        while True:
            time.sleep(INTERVAL)
            try:
                self.sample()
            except Exception as e:  # keep the dashboard alive whatever the OS reports
                print("sample failed:", repr(e), file=sys.stderr)

    def sample(self):
        P = self.platform
        now = time.monotonic()
        dt, self.t = max(0.1, now - self.t), now
        total, cores = P.cpu_usage()

        procs = P.list_procs()
        for p in procs.values():
            prev = self.cpu_times.get(p["key"])
            p["cpu"] = round(max(0.0, p["cpu_time"] - prev) / dt * 100, 1) if prev is not None else 0.0
        self.cpu_times = {p["key"]: p["cpu_time"] for p in procs.values()}
        if total is None:
            total = round(min(100.0, sum(p["cpu"] for p in procs.values()) / self.ncpu), 1)

        conf = self.get_conf()
        targets = guard_targets(procs, conf, self.uid, self.self_pid)
        if conf["guard_enabled"]:
            for pid in targets:
                if procs[pid]["nice"] < conf["guard_nice"] and renice(pid, conf["guard_nice"]):
                    procs[pid]["nice"] = conf["guard_nice"]
                    self.reniced += 1

        mem = P.memory()
        pressure = P.pressure()
        low = memory_low(mem, pressure, conf["rescue_min_available_pct"])
        if low and conf["rescue_enabled"] and now - self.last_rescue >= RESCUE_COOLDOWN:
            victim = rescue_target(procs, targets, conf)
            if victim:
                try:
                    os.kill(victim["pid"], signal.SIGTERM)
                    self.last_rescue = now
                    self.events.appendleft({"time": time.time(), "name": victim["comm"],
                                            "pid": victim["pid"], "rss": victim["rss"]})
                except OSError:
                    pass

        groups = {}
        for p in procs.values():
            g = groups.setdefault(p["comm"], {"name": p["comm"], "cpu": 0.0, "rss": 0, "count": 0, "procs": []})
            g["cpu"] += p["cpu"]
            g["rss"] += p["rss"]
            g["count"] += 1
            g["procs"].append(p)
        by_cpu = sorted(groups.values(), key=lambda g: -g["cpu"])[:20]
        by_rss = sorted(groups.values(), key=lambda g: -g["rss"])[:20]
        top = list({g["name"]: g for g in by_cpu + by_rss}.values())
        out_groups = []
        for g in top:
            out_groups.append({
                "name": g["name"], "cpu": round(g["cpu"], 1), "rss": g["rss"], "count": g["count"],
                "guarded": any(p["pid"] in targets for p in g["procs"]),
                "protected": g["name"] in PROTECTED,
                "procs": [{
                    "pid": p["pid"], "cpu": p["cpu"], "rss": p["rss"], "nice": p["nice"],
                    "own": p["uid"] == self.uid, "guarded": p["pid"] in targets, "cmd": P.cmdline(p["pid"]),
                } for p in sorted(g["procs"], key=lambda p: (-p["cpu"], -p["rss"]))[:8]],
            })

        temp = P.temperature()
        freq, freq_max = P.frequency()
        mem_pct = round(100 * (mem["total"] - mem["available"]) / mem["total"], 1) if mem["total"] else None
        self.history.append({"cpu": total, "mem": mem_pct, "temp": temp})
        with self.lock:
            self.snapshot = {
                "time": time.time(), "platform": P.name, "ncpu": self.ncpu, "cpu": total, "cores": cores,
                "temp": temp, "freq": freq, "freq_max": freq_max, "mem": mem, "mem_pct": mem_pct,
                "memory_low": low, "pressure": pressure, "load": os.getloadavg(), "groups": out_groups,
                "guard": {"enabled": conf["guard_enabled"], "nice": conf["guard_nice"],
                          "active": len(targets), "reniced": self.reniced,
                          "cpu": round(sum(procs[p]["cpu"] for p in targets), 1),
                          "rss": sum(procs[p]["rss"] for p in targets)},
                "rescue": {"enabled": conf["rescue_enabled"], "events": list(self.events)},
                "history": list(self.history),
            }

    def state(self):
        with self.lock:
            return self.snapshot
