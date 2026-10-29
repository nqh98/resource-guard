"""Smoke tests against the real OS this runs on (CI runs them on Linux and macOS)."""
import os
import unittest

from resource_guard.monitor import Monitor
from resource_guard.server import default_conf, get_platform


class PlatformSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.P = get_platform()

    def test_sees_this_process(self):
        procs = self.P.list_procs()
        me = procs.get(os.getpid())
        self.assertIsNotNone(me)
        self.assertEqual(me["uid"], os.getuid())
        self.assertEqual(me["ppid"], os.getppid())
        self.assertGreater(me["rss"], 0)
        self.assertGreaterEqual(me["cpu_time"], 0)

    def test_memory(self):
        mem = self.P.memory()
        self.assertGreater(mem["total"], 2 ** 28)
        self.assertGreater(mem["available"], 0)
        self.assertLessEqual(mem["available"], mem["total"])

    def test_monitor_sample(self):
        mem_total = self.P.memory()["total"]
        conf = default_conf(os.cpu_count() or 1, mem_total)
        m = Monitor(self.P, lambda: conf)
        sum(range(2_000_000))  # burn a little CPU so there is something to measure
        m.sample()
        s = m.state()
        self.assertEqual(s["platform"], self.P.name)
        self.assertTrue(0 <= s["cpu"] <= 100)
        self.assertTrue(s["groups"])
        self.assertIn("memory_level", s["pressure"])

    def test_cap_prefix(self):
        conf = default_conf(8, 32 * 2 ** 30)
        prefix = self.P.cap_prefix(conf)
        self.assertIn("nice -n 10", prefix)


if __name__ == "__main__":
    unittest.main()
