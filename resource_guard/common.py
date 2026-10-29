"""Helpers shared by every platform backend."""
import os
import re
import subprocess
import sys
from pathlib import Path

# Names users may put in the guard lists: plain process names, nothing a shell would interpret.
NAME_RE = re.compile(r"[\w.+-]{1,32}")

# Stopping any of these ends the desktop session or breaks sound and input.
PROTECTED = {
    # Linux
    "systemd", "gnome-shell", "gnome-session-b", "gnome-session-binary", "Xwayland", "Xorg",
    "dbus-daemon", "dbus-broker", "pipewire", "pipewire-pulse", "wireplumber", "gdm-wayland-ses",
    "gdm-x-session", "ibus-daemon", "mutter-x11-fram", "plasmashell", "kwin_wayland", "kwin_x11",
    # macOS
    "launchd", "kernel_task", "WindowServer", "loginwindow", "Dock", "Finder", "SystemUIServer",
    "coreaudiod", "ControlCenter",
}


class ActionError(Exception):
    """A refused action. The dashboard translates `code`; `params` fill in its message."""

    def __init__(self, code, **params):
        super().__init__(code)
        self.code = code
        self.params = params


def read_file(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def run(cmd, timeout=10):
    """Run a command; return (exit code, combined output). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def renice(pid, value):
    """Lower the priority of every thread of pid to value. Never raises it.

    On Linux setpriority() acts on one thread, so walk /proc/<pid>/task; elsewhere the
    whole process is reniced at once.
    """
    changed = False
    try:
        tids = [int(t) for t in os.listdir(f"/proc/{pid}/task")]
    except OSError:
        tids = [pid]
    for tid in tids:
        try:
            if os.getpriority(os.PRIO_PROCESS, tid) < value:
                os.setpriority(os.PRIO_PROCESS, tid, value)
                changed = True
        except OSError:
            pass
    return changed


def parse_cputime(text):
    """'1:02:03.45', '12:34.56' or '1-02:03:04' (days) -> seconds."""
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        days = int(d)
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return days * 86400 + seconds


def config_dir():
    return Path.home() / ".config" / "resource-guard"


def launch_command():
    """How to start this program again from a service manager: (argv, PYTHONPATH)."""
    package_parent = Path(__file__).resolve().parent.parent
    return [sys.executable, "-m", "resource_guard"], str(package_parent)
