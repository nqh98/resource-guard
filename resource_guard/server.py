"""Dashboard server: config, actions and a localhost-only HTTP API."""
import argparse
import errno
import json
import os
import secrets
import signal
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, shellrc
from .common import NAME_RE, PROTECTED, ActionError, config_dir, read_file, renice
from .monitor import Monitor

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STATIC = Path(__file__).resolve().parent / "static"
MEM_CHOICES_GB = (0, 2, 4, 6, 8, 12, 16, 24, 32, 48, 64)
RESCUE_CHOICES_PCT = (3, 5, 10)


def get_platform():
    if sys.platform.startswith("linux"):
        from .linux import LinuxPlatform
        return LinuxPlatform()
    if sys.platform == "darwin":
        from .macos import MacPlatform
        return MacPlatform()
    sys.exit(f"Resource Guard supports Linux and macOS, not {sys.platform}.")


def default_conf(ncpu, mem_total):
    total_gb = mem_total // 2 ** 30
    return {
        "guard_enabled": True,
        "guard_nice": 10,
        # These processes and everything they start get the lower priority and the limits.
        "guard_roots": ["claude", "codex"],
        # Build and check tools that use every core, wherever they were started.
        "guard_names": ["tsc", "node", "esbuild", "vite", "webpack", "jest", "vitest",
                        "go", "gopls", "compile", "link", "golangci-lint",
                        "cargo", "rustc", "make", "pytest"],
        # About five of every eight threads: builds stay fast, the desktop keeps the rest.
        "cpu_quota": max(100, round(ncpu * 0.625) * 100),
        # Soft memory limit per agent: above it the agent is slowed and reclaimed, not killed.
        "mem_high_gb": max(c for c in MEM_CHOICES_GB if c <= max(2, total_gb // 4)),
        "mac_clamp": "utility",
        "rescue_enabled": True,
        "rescue_min_available_pct": 5,
    }


class App:
    """Config plus every action the dashboard can take."""

    def __init__(self, platform):
        self.P = platform
        self.ncpu = os.cpu_count() or 1
        self.lock = threading.Lock()
        self.conf = default_conf(self.ncpu, platform.memory()["total"])
        try:
            saved = json.loads((config_dir() / "config.json").read_text())
            self.conf.update({k: v for k, v in saved.items() if k in self.conf})
        except (OSError, ValueError):
            pass
        self.monitor = Monitor(platform, self.get_conf)

    def get_conf(self):
        with self.lock:
            return dict(self.conf)

    def save_conf(self):
        path = config_dir() / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.conf, indent=2))

    # ------------------------------------------------------------ state

    def choices(self):
        mem_total = self.P.memory()["total"]
        return {
            "cpu_quota": [q * 100 for q in range(1, self.ncpu + 1)],
            "mem_high_gb": [c for c in MEM_CHOICES_GB if c * 2 ** 30 < mem_total],
            "mac_clamp": ["utility", "background"],
            "rescue_min_available_pct": list(RESCUE_CHOICES_PCT),
        }

    def settings(self):
        conf = self.get_conf()
        rc = shellrc.rc_file()
        return {
            "version": __version__, "platform": self.P.name, "ncpu": self.ncpu, "conf": conf,
            "choices": self.choices(),
            "alias": {"installed": bool(rc) and shellrc.installed(rc), "file": f"~/{rc.name}" if rc else None,
                      "available": bool(rc) and self.P.cap_available(), "commands": conf["guard_roots"]},
            "desktop": self.P.desktop_state(),
            "power": self.P.power(),
            "autostart": self.P.autostart_enabled(),
            "zram": self.P.zram(),
        }

    # ------------------------------------------------------------ helpers

    def _own_pid(self, body):
        try:
            pid = int(body.get("pid"))
        except (TypeError, ValueError):
            raise ActionError("bad_pid")
        if pid <= 1 or pid == os.getpid():
            raise ActionError("cannot_touch")
        procs = self.P.list_procs()
        if pid not in procs:
            raise ActionError("gone")
        if procs[pid]["uid"] != os.getuid():
            raise ActionError("other_user")
        return pid, procs[pid]["comm"]

    def _apply_limits(self, conf):
        """Rewrite the installed limits after the config changed."""
        rc = shellrc.rc_file()
        if rc and shellrc.installed(rc):
            shellrc.write(rc, shellrc.block(self.P.cap_prefix(conf), conf["guard_roots"]))
        if self.P.desktop_state()["installed"]:
            self.P.set_desktop(True, conf)

    # ------------------------------------------------------------ actions

    def act_renice(self, body):
        pid, _ = self._own_pid(body)
        value = int(body.get("nice", 15))
        if not 1 <= value <= 19:
            raise ActionError("bad_nice")
        renice(pid, value)

    def act_kill(self, body):
        pid, comm = self._own_pid(body)
        if comm in PROTECTED:
            raise ActionError("protected", name=comm)
        os.kill(pid, signal.SIGTERM)

    def act_kill_name(self, body):
        name = body.get("name")
        if not isinstance(name, str) or not 0 < len(name) <= 128:
            raise ActionError("bad_name")
        if name in PROTECTED:
            raise ActionError("protected", name=name)
        n = 0
        for p in self.P.list_procs().values():
            if p["comm"] == name and p["uid"] == os.getuid() and p["pid"] not in (1, os.getpid()):
                try:
                    os.kill(p["pid"], signal.SIGTERM)
                    n += 1
                except OSError:
                    pass
        return {"ok": True, "killed": n}

    def act_power(self, body):
        self.P.set_power(body.get("profile"))

    def act_config(self, body):
        choices = self.choices()
        with self.lock:
            new = dict(self.conf)
            for key in ("guard_enabled", "rescue_enabled"):
                if key in body:
                    new[key] = bool(body[key])
            if "guard_nice" in body:
                new["guard_nice"] = int(body["guard_nice"])
                if not 1 <= new["guard_nice"] <= 19:
                    raise ActionError("bad_nice")
            for key in ("cpu_quota", "mem_high_gb", "mac_clamp", "rescue_min_available_pct"):
                if key in body:
                    if body[key] not in choices[key]:
                        raise ActionError("bad_choice")
                    new[key] = body[key]
            for key in ("guard_roots", "guard_names"):
                if key in body:
                    v = body[key]
                    if not isinstance(v, list) or not all(isinstance(s, str) and NAME_RE.fullmatch(s) for s in v):
                        raise ActionError("bad_list")
                    new[key] = list(dict.fromkeys(v))
            self.conf = new
            self.save_conf()
        self._apply_limits(new)

    def act_alias(self, body):
        rc = shellrc.rc_file()
        if not rc:
            raise ActionError("unsupported_shell")
        if body.get("install"):
            if not self.P.cap_available():
                raise ActionError("unsupported")
            conf = self.get_conf()
            shellrc.write(rc, shellrc.block(self.P.cap_prefix(conf), conf["guard_roots"]))
        else:
            shellrc.write(rc, None)

    def act_desktop(self, body):
        self.P.set_desktop(bool(body.get("install")), self.get_conf())

    def act_autostart(self, body):
        self.P.set_autostart(bool(body.get("enable")))

    ACTIONS = {
        "/api/renice": act_renice, "/api/kill": act_kill, "/api/kill-name": act_kill_name,
        "/api/power": act_power, "/api/config": act_config, "/api/alias": act_alias,
        "/api/desktop": act_desktop, "/api/autostart": act_autostart,
    }


def make_handler(app, port, token):
    allowed_hosts = (f"127.0.0.1:{port}", f"localhost:{port}")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            # Only our own address may talk to us: blocks DNS rebinding.
            if self.headers.get("Host") not in allowed_hosts:
                return self.send(403, {"error": "forbidden"})
            path = self.path.split("?")[0]
            if path == "/":
                html = read_file(STATIC / "index.html").replace("__TOKEN__", token)
                self.send(200, html.encode(), "text/html")
            elif path == "/api/state":
                self.send(200, app.monitor.state())
            elif path == "/api/settings":
                self.send(200, app.settings())
            else:
                self.send(404, {"error": "not_found"})

        def do_POST(self):
            # The custom header forces a CORS preflight we never answer, so other sites can't post.
            if (self.headers.get("Host") not in allowed_hosts
                    or not secrets.compare_digest(self.headers.get("X-Token", ""), token)):
                return self.send(403, {"error": "forbidden"})
            action = App.ACTIONS.get(self.path)
            if not action:
                return self.send(404, {"error": "not_found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
                self.send(200, action(app, body) or {"ok": True})
            except ActionError as e:
                self.send(400, {"error": e.code, "params": e.params})
            except (ValueError, TypeError):
                self.send(400, {"error": "bad_request"})
            except OSError as e:
                self.send(400, {"error": "command_failed", "params": {"detail": e.strerror or str(e)}})

    return Handler


def create_server(app, port):
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((HOST, port), None)
    server.RequestHandlerClass = make_handler(app, server.server_address[1], token)
    return server, token


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="resource-guard", description="Keep AI agents and their builds from freezing your desktop.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("RESOURCE_GUARD_PORT", DEFAULT_PORT)))
    parser.add_argument("--open", action="store_true", help="open the dashboard in the browser")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    renice(os.getpid(), 5)  # the guard itself should never compete with the desktop
    app = App(get_platform())
    try:
        server, _ = create_server(app, args.port)
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            # Usually another Resource Guard. Exit 0 so service managers don't keep retrying.
            print(f"Port {args.port} is in use; Resource Guard is probably already running.")
            return
        raise
    time.sleep(1)  # the first sample needs a real interval to measure CPU against
    app.monitor.sample()
    app.monitor.start()
    url = f"http://{HOST}:{server.server_address[1]}"
    print(f"Resource Guard {__version__}: {url}  (Ctrl+C to stop)", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
