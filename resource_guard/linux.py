"""Linux backend: /proc for numbers, systemd scopes for CPU and memory limits."""
import os
import re
import shutil
from pathlib import Path

from .common import ActionError, launch_command, read_file, run

SERVICE_NAME = "resource-guard.service"
# Claude Desktop is started from the app menu, so a shell alias never wraps it. GNOME puts
# each launch in its own app-com.anthropic.Claude-<pid>.scope; a prefix drop-in caps them all.
DESKTOP_PREFIX = "app-com.anthropic.Claude-"


def _systemd_user_dir():
    return Path.home() / ".config" / "systemd" / "user"


def _desktop_dropin():
    return _systemd_user_dir() / f"{DESKTOP_PREFIX}.scope.d" / "resource-guard.conf"


def _psi(kind):
    m = re.search(r"some avg10=([\d.]+)", read_file(f"/proc/pressure/{kind}"))
    return float(m.group(1)) if m else None


class LinuxPlatform:
    name = "linux"

    def __init__(self):
        self.clk = os.sysconf("SC_CLK_TCK")
        self.page = os.sysconf("SC_PAGE_SIZE")
        self._cpus = self._cpu_times()

    # ------------------------------------------------------------ numbers

    @staticmethod
    def _cpu_times():
        """[(busy, total)] for the whole CPU, then one per core."""
        out = []
        for line in read_file("/proc/stat").splitlines():
            if not line.startswith("cpu"):
                break
            v = [int(x) for x in line.split()[1:9]]
            idle = v[3] + v[4]
            out.append((sum(v) - idle, sum(v)))
        return out

    def cpu_usage(self):
        """(total %, [per-core %]) since the previous call."""
        cpus = self._cpu_times()
        usage = [round(100 * (b - pb) / max(1, t - pt), 1) for (b, t), (pb, pt) in zip(cpus, self._cpus)]
        self._cpus = cpus
        return (usage[0], usage[1:]) if usage else (None, [])

    def list_procs(self):
        procs = {}
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            raw = read_file(f"/proc/{pid}/stat")
            if not raw:
                continue
            r = raw.rfind(")")
            f = raw[r + 2:].split()
            ppid = int(f[1])
            if pid == 2 or ppid == 2:
                continue  # kernel threads
            try:
                uid = os.stat(f"/proc/{pid}").st_uid
            except OSError:
                continue
            procs[pid] = {
                "pid": pid, "comm": raw[raw.find("(") + 1:r], "ppid": ppid, "uid": uid,
                "cpu_time": (int(f[11]) + int(f[12])) / self.clk, "nice": int(f[16]),
                "key": (pid, int(f[19])), "rss": int(f[21]) * self.page,
            }
        return procs

    def cmdline(self, pid):
        return read_file(f"/proc/{pid}/cmdline").replace("\0", " ").strip()[:200]

    def temperature(self):
        best = fallback = None
        for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
            kind = read_file(zone / "type").strip()
            try:
                c = int(read_file(zone / "temp").strip()) / 1000
            except ValueError:
                continue
            if not 0 < c < 130:
                continue
            if kind in ("x86_pkg_temp", "TCPU", "k10temp", "cpu-thermal"):
                best = c if best is None else max(best, c)
            fallback = c if fallback is None else max(fallback, c)
        return best if best is not None else fallback

    def frequency(self):
        cur = []
        for p in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"):
            v = read_file(p).strip()
            if v.isdigit():
                cur.append(int(v))
        mx = read_file("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq").strip()
        return (round(sum(cur) / len(cur) / 1e6, 2) if cur else None,
                round(int(mx) / 1e6, 2) if mx.isdigit() else None)

    def memory(self):
        m = {}
        for line in read_file("/proc/meminfo").splitlines():
            k, _, v = line.partition(":")
            m[k] = int(v.split()[0]) * 1024 if v.strip() else 0
        return {"total": m.get("MemTotal", 0), "available": m.get("MemAvailable", 0),
                "swap_total": m.get("SwapTotal", 0),
                "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0)}

    def pressure(self):
        return {"cpu": _psi("cpu"), "memory": _psi("memory"), "memory_level": None}

    def zram(self):
        """Compressed swap in RAM: much faster than a swap file when memory runs short."""
        lines = read_file("/proc/swaps").splitlines()[1:]
        size = sum(int(l.split()[2]) * 1024 for l in lines if l.startswith("/dev/zram"))
        return {"active": size > 0, "size": size}

    # ------------------------------------------------------------ power

    def power(self):
        if not shutil.which("powerprofilesctl"):
            return {"current": None, "profiles": []}
        _, listing = run(["powerprofilesctl", "list"])
        _, current = run(["powerprofilesctl", "get"])
        return {"current": current.strip(),
                "profiles": re.findall(r"^\s*\*?\s*([a-z-]+):\s*$", listing, re.M)}

    def set_power(self, profile):
        if profile not in self.power()["profiles"]:
            raise ActionError("bad_profile")
        code, out = run(["powerprofilesctl", "set", profile])
        if code:
            raise ActionError("command_failed", detail=out)

    # ------------------------------------------------------------ limits

    def cap_available(self):
        return bool(shutil.which("systemd-run"))

    def cap_prefix(self, conf):
        """Shell words that start a command inside a CPU- and memory-capped scope."""
        props = f"-p CPUQuota={conf['cpu_quota']}% -p CPUWeight=20"
        if conf["mem_high_gb"]:
            props += f" -p MemoryHigh={conf['mem_high_gb']}G"
        return f"systemd-run --user --scope --quiet --collect {props} nice -n {conf['guard_nice']}"

    def desktop_scopes(self):
        _, out = run(["systemctl", "--user", "list-units", "--type=scope", "--plain", "--no-legend",
                      f"{DESKTOP_PREFIX}*"])
        return [line.split()[0] for line in out.splitlines() if line.startswith(DESKTOP_PREFIX)]

    def desktop_state(self):
        return {"available": bool(shutil.which("claude-desktop")), "installed": _desktop_dropin().exists(),
                "running": len(self.desktop_scopes())}

    def set_desktop(self, install, conf):
        """Cap Claude Desktop for future launches (drop-in) and for running instances."""
        dropin = _desktop_dropin()
        mem = f"{conf['mem_high_gb']}G" if conf["mem_high_gb"] else "infinity"
        if install:
            dropin.parent.mkdir(parents=True, exist_ok=True)
            dropin.write_text(
                "# Added by Resource Guard: cap Claude Desktop and the agents it runs.\n"
                f"[Scope]\nCPUQuota={conf['cpu_quota']}%\nCPUWeight=20\nMemoryHigh={mem}\n")
            props = [f"CPUQuota={conf['cpu_quota']}%", "CPUWeight=20", f"MemoryHigh={mem}"]
        else:
            dropin.unlink(missing_ok=True)
            props = ["CPUQuota=", "CPUWeight=", "MemoryHigh=infinity"]  # back to unlimited
        run(["systemctl", "--user", "daemon-reload"])
        for scope in self.desktop_scopes():
            code, out = run(["systemctl", "--user", "set-property", "--runtime", scope, *props])
            if code:
                raise ActionError("command_failed", detail=out)

    # ------------------------------------------------------------ autostart

    def autostart_enabled(self):
        code, _ = run(["systemctl", "--user", "is-enabled", SERVICE_NAME])
        return code == 0

    def set_autostart(self, enable):
        unit = _systemd_user_dir() / SERVICE_NAME
        if enable:
            argv, pythonpath = launch_command()
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(
                "[Unit]\nDescription=Resource Guard dashboard\n\n"
                "[Service]\n"
                f"ExecStart={' '.join(argv)}\n"
                f"Environment=PYTHONPATH={pythonpath}\n"
                "Restart=on-failure\n\n"
                "[Install]\nWantedBy=default.target\n")
            run(["systemctl", "--user", "daemon-reload"])
            code, out = run(["systemctl", "--user", "enable", SERVICE_NAME])
        else:
            code, out = run(["systemctl", "--user", "disable", SERVICE_NAME])
            unit.unlink(missing_ok=True)
            run(["systemctl", "--user", "daemon-reload"])
        if code:
            raise ActionError("command_failed", detail=out)
