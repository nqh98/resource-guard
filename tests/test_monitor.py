import unittest

from resource_guard.monitor import RESCUE_MIN_RSS, guard_targets, memory_low, rescue_target

UID = 1000
CONF = {"guard_roots": ["claude"], "guard_names": ["tsc"]}


def proc(pid, ppid, comm, uid=UID, rss=0):
    return {"pid": pid, "ppid": ppid, "comm": comm, "uid": uid, "rss": rss}


PROCS = {p["pid"]: p for p in [
    proc(1, 0, "systemd", uid=0),
    proc(10, 1, "bash"),
    proc(11, 10, "claude"),
    proc(12, 11, "bash"),
    proc(13, 12, "node", rss=3 * 2 ** 30),       # a build the agent started
    proc(14, 13, "esbuild", rss=RESCUE_MIN_RSS),
    proc(20, 10, "tsc", rss=2 ** 30),            # a build tool started by hand
    proc(21, 10, "chrome", rss=8 * 2 ** 30),
    proc(30, 1, "tsc", uid=0),                   # someone else's
    proc(40, 999, "node"),                       # parent unknown
]}


class GuardTargetsTest(unittest.TestCase):
    def test_agent_tree_and_build_tools(self):
        self.assertEqual(guard_targets(PROCS, CONF, UID, self_pid=0), {11, 12, 13, 14, 20})

    def test_never_targets_itself(self):
        self.assertNotIn(13, guard_targets(PROCS, CONF, UID, self_pid=13))

    def test_parent_cycle_terminates(self):
        procs = {1: proc(1, 2, "a"), 2: proc(2, 1, "b")}
        self.assertEqual(guard_targets(procs, CONF, UID, self_pid=0), set())


class RescueTest(unittest.TestCase):
    def test_picks_biggest_build_never_the_agent_or_other_apps(self):
        targets = guard_targets(PROCS, CONF, UID, self_pid=0)
        self.assertEqual(rescue_target(PROCS, targets, CONF)["pid"], 13)

    def test_nothing_big_enough(self):
        procs = {11: proc(11, 1, "claude", rss=10 * 2 ** 30), 12: proc(12, 11, "node", rss=10)}
        self.assertIsNone(rescue_target(procs, {11, 12}, CONF))

    def test_memory_low(self):
        mem = {"total": 100, "available": 4}
        self.assertTrue(memory_low(mem, {}, 5))
        self.assertFalse(memory_low(mem, {}, 3))
        self.assertTrue(memory_low({"total": 100, "available": 90}, {"memory_level": "critical"}, 5))
        self.assertFalse(memory_low({"total": 0, "available": 0}, {}, 5))


if __name__ == "__main__":
    unittest.main()
