import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from resource_guard import shellrc
from resource_guard.common import ActionError
from resource_guard.server import App, create_server

GB = 2 ** 30


class FakePlatform:
    """Stands in for the OS so the tests never touch systemd, launchd or real processes."""
    name = "linux"

    def __init__(self, procs=None):
        self.procs = procs or {}
        self.power_set = None

    def cpu_usage(self): return 10.0, [10.0, 10.0]
    def list_procs(self): return {pid: dict(p) for pid, p in self.procs.items()}
    def cmdline(self, pid): return ""
    def temperature(self): return None
    def frequency(self): return None, None
    def memory(self): return {"total": 16 * GB, "available": 8 * GB, "swap_total": 0, "swap_used": 0}
    def pressure(self): return {"cpu": 1.0, "memory": 0.0, "memory_level": None}
    def zram(self): return None
    def power(self): return {"current": "balanced", "profiles": ["balanced", "power-saver"]}
    def set_power(self, profile):
        if profile not in self.power()["profiles"]:
            raise ActionError("bad_profile")
        self.power_set = profile
    def cap_available(self): return True
    def cap_prefix(self, conf): return f"nice -n {conf['guard_nice']}"
    def desktop_state(self): return {"available": False, "installed": False, "running": 0}
    def set_desktop(self, install, conf): pass
    def autostart_enabled(self): return False
    def set_autostart(self, enable): pass


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home.name
        self.sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        me = os.getuid()
        self.platform = FakePlatform({
            self.sleeper.pid: {"pid": self.sleeper.pid, "ppid": 1, "comm": "sleeper", "uid": me, "rss": 0,
                               "nice": 0, "cpu_time": 0.0, "key": (self.sleeper.pid, 0)},
            4242: {"pid": 4242, "ppid": 1, "comm": "gnome-shell", "uid": me, "rss": 0,
                   "nice": 0, "cpu_time": 0.0, "key": (4242, 0)},
        })
        self.app = App(self.platform)
        self.app.monitor.sample()
        self.server, _ = create_server(self.app, 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        page = self.get("/")[1]
        self.token = re.search(r'TOKEN = "([^"]+)"', page).group(1)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.sleeper.kill()
        self.sleeper.wait()
        if self.old_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = self.old_home
        self.home.cleanup()

    def request(self, method, path, body=None, host=None, token=True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}", "Content-Type": "application/json"}
        if token and method == "POST":
            headers["X-Token"] = self.token
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = conn.getresponse()
        data = r.read().decode()
        conn.close()
        return r.status, data

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body, **kw):
        status, data = self.request("POST", path, body, **kw)
        return status, json.loads(data)

    def test_page_and_state(self):
        status, page = self.get("/")
        self.assertEqual(status, 200)
        self.assertNotIn("__TOKEN__", page)
        status, data = self.get("/api/state")
        self.assertEqual(json.loads(data)["platform"], "linux")
        settings = json.loads(self.get("/api/settings")[1])
        self.assertEqual(settings["conf"]["mem_high_gb"], 4)  # a quarter of 16 GB

    def test_rejects_other_hosts_and_missing_token(self):
        self.assertEqual(self.get("/api/state", host="evil.example")[0], 403)
        self.assertEqual(self.post("/api/power", {"profile": "balanced"}, token=False)[0], 403)
        self.assertEqual(self.post("/api/power", {"profile": "balanced"}, host="evil.example")[0], 403)
        self.assertIsNone(self.platform.power_set)

    def test_config_validation_and_persistence(self):
        status, body = self.post("/api/config", {"cpu_quota": 12345})
        self.assertEqual((status, body["error"]), (400, "bad_choice"))
        status, body = self.post("/api/config", {"guard_roots": ["rm -rf /"]})
        self.assertEqual((status, body["error"]), (400, "bad_list"))
        status, _ = self.post("/api/config", {"mem_high_gb": 8, "rescue_min_available_pct": 10})
        self.assertEqual(status, 200)
        saved = json.loads((Path(self.home.name) / ".config/resource-guard/config.json").read_text())
        self.assertEqual((saved["mem_high_gb"], saved["rescue_min_available_pct"]), (8, 10))

    def test_renice_and_stop(self):
        pid = self.sleeper.pid
        self.assertEqual(self.post("/api/renice", {"pid": pid, "nice": 15})[0], 200)
        self.assertEqual(os.getpriority(os.PRIO_PROCESS, pid), 15)
        self.assertEqual(self.post("/api/kill", {"pid": pid})[0], 200)
        self.sleeper.wait(timeout=5)

    def test_refuses_desktop_and_invalid_targets(self):
        status, body = self.post("/api/kill-name", {"name": "gnome-shell"})
        self.assertEqual((status, body["error"], body["params"]["name"]), (400, "protected", "gnome-shell"))
        self.assertEqual(self.post("/api/kill", {"pid": 4242})[1]["error"], "protected")
        self.assertEqual(self.post("/api/kill", {"pid": 1})[1]["error"], "cannot_touch")
        self.assertEqual(self.post("/api/kill", {"pid": "x"})[1]["error"], "bad_pid")
        self.assertEqual(self.post("/api/power", {"profile": "turbo"})[1]["error"], "bad_profile")

    def test_alias_follows_config(self):
        rc = shellrc.rc_file()
        if rc is None:
            self.skipTest("login shell is neither bash nor zsh")
        self.assertEqual(self.post("/api/alias", {"install": True})[0], 200)
        self.assertIn("nice -n 10", rc.read_text())
        self.post("/api/config", {"guard_nice": 15})
        self.assertIn("nice -n 15", rc.read_text())
        self.post("/api/alias", {"install": False})
        self.assertFalse(shellrc.installed(rc))


if __name__ == "__main__":
    unittest.main()
