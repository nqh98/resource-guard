"""macOS backend: ps, vm_stat and sysctl for numbers, taskpolicy QoS clamps for limits.

macOS has no cgroups, so there is no hard CPU or memory cap. Clamping a process to the
"utility" or "background" QoS class is the native way to make work yield to the UI; on
Apple Silicon "background" also keeps it on the efficiency cores.
"""
import os
import plistlib
import re
import shutil
from pathlib import Path

from .common import ActionError, launch_command, parse_cputime, run

LABEL = "io.github.resource-guard"
CLAMPS = ("utility", "background")
PRESSURE_LEVELS = {1: "normal", 2: "warn", 4: "critical"}


def parse_ps(out):
    """Parse `ps -axo pid=,ppid=,uid=,nice=,rss=,time=,comm=`. comm (last) may contain spaces."""
    procs = {}
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        pid, ppid, uid, nice, rss, cputime, comm = parts
        try:
            pid = int(pid)
            procs[pid] = {
                "pid": pid, "ppid": int(ppid), "uid": int(uid), "nice": int(nice),
                "rss": int(rss) * 1024, "cpu_time": parse_cputime(cputime),
                "comm": os.path.basename(comm.strip()), "key": (pid, comm),
            }
        except ValueError:
            continue
    return procs


def parse_args(out):
    """Parse `ps -axww -o pid=,args=` into {pid: command line}."""
    result = {}
    for line in out.splitlines():
        pid, _, args = line.strip().partition(" ")
        if pid.isdigit():
            result[int(pid)] = args.strip()[:200]
    return result


def parse_vm_stat(out):
    """`vm_stat` -> (page size, {"free": pages, "inactive": pages, ...})."""
    m = re.search(r"page size of (\d+) bytes", out)
    page = int(m.group(1)) if m else 4096
    pages = {}
    for line in out.splitlines():
        k, _, v = line.partition(":")
        v = v.strip().rstrip(".")
        if v.isdigit():
            pages[k.replace("Pages", "").strip()] = int(v)
    return page, pages


def parse_swapusage(out):
    """'total = 2048.00M  used = 1024.50M  free = ...' -> (total bytes, used bytes)."""
    units = {"K": 2 ** 10, "M": 2 ** 20, "G": 2 ** 30}
    vals = dict(re.findall(r"(total|used) = ([\d.]+[KMG])", out))

    def to_bytes(s):
        return int(float(s[:-1]) * units[s[-1]]) if s else 0

    return to_bytes(vals.get("total")), to_bytes(vals.get("used"))


def _sysctl(name):
    code, out = run(["sysctl", "-n", name])
    return out if code == 0 else ""


def _agent_plist():
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


class MacPlatform:
    name = "macos"

    def __init__(self):
        self._args = {}
        total = _sysctl("hw.memsize")
        self._total = int(total) if total.isdigit() else 0

    # ------------------------------------------------------------ numbers

    def cpu_usage(self):
        return None, []  # the monitor derives the total from per-process deltas

    def list_procs(self):
        _, out = run(["ps", "-axo", "pid=,ppid=,uid=,nice=,rss=,time=,comm="])
        _, args = run(["ps", "-axww", "-o", "pid=,args="])
        self._args = parse_args(args)
        return parse_ps(out)

    def cmdline(self, pid):
        return self._args.get(pid, "")

    def temperature(self):
        return None  # needs root (powermetrics)

    def frequency(self):
        return None, None

    def memory(self):
        _, out = run(["vm_stat"])
        page, pages = parse_vm_stat(out)
        available = sum(pages.get(k, 0) for k in ("free", "inactive", "speculative")) * page
        swap_total, swap_used = parse_swapusage(_sysctl("vm.swapusage"))
        return {"total": self._total, "available": min(available, self._total),
                "swap_total": swap_total, "swap_used": swap_used}

    def pressure(self):
        level = _sysctl("kern.memorystatus_vm_pressure_level")
        return {"cpu": None, "memory": None,
                "memory_level": PRESSURE_LEVELS.get(int(level)) if level.isdigit() else None}

    def zram(self):
        return None  # macOS compresses memory on its own

    # ------------------------------------------------------------ power

    def power(self):
        # Low Power Mode can only be switched with sudo, so it is shown, not changed.
        _, out = run(["pmset", "-g"])
        m = re.search(r"lowpowermode\s+(\d)", out)
        return {"current": ("low-power" if m.group(1) == "1" else "normal") if m else None, "profiles": []}

    def set_power(self, profile):
        raise ActionError("unsupported")

    # ------------------------------------------------------------ limits

    def cap_available(self):
        return bool(shutil.which("taskpolicy"))

    def cap_prefix(self, conf):
        clamp = conf["mac_clamp"] if conf["mac_clamp"] in CLAMPS else "utility"
        return f"taskpolicy -c {clamp} nice -n {conf['guard_nice']}"

    def desktop_state(self):
        return {"available": False, "installed": False, "running": 0}

    def set_desktop(self, install, conf):
        raise ActionError("unsupported")

    # ------------------------------------------------------------ autostart

    def autostart_enabled(self):
        return _agent_plist().exists()

    def set_autostart(self, enable):
        plist = _agent_plist()
        domain = f"gui/{os.getuid()}"
        if enable:
            argv, pythonpath = launch_command()
            plist.parent.mkdir(parents=True, exist_ok=True)
            with open(plist, "wb") as f:
                plistlib.dump({
                    "Label": LABEL, "ProgramArguments": argv, "RunAtLoad": True,
                    "EnvironmentVariables": {"PYTHONPATH": pythonpath},
                    "KeepAlive": {"SuccessfulExit": False},
                }, f)
            run(["launchctl", "bootout", f"{domain}/{LABEL}"])
            # Registers the agent for future logins. It also starts it now; if this instance
            # already holds the port, the new one exits cleanly and launchd leaves it stopped.
            code, out = run(["launchctl", "bootstrap", domain, str(plist)])
        else:
            code, out = run(["launchctl", "bootout", f"{domain}/{LABEL}"])
            code = 0  # not loaded is fine
            plist.unlink(missing_ok=True)
        if code:
            raise ActionError("command_failed", detail=out)
