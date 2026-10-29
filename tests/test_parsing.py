import unittest

from resource_guard.common import parse_cputime
from resource_guard.macos import parse_args, parse_ps, parse_swapusage, parse_vm_stat


class CputimeTest(unittest.TestCase):
    def test_formats(self):
        self.assertAlmostEqual(parse_cputime("0:01.50"), 1.5)
        self.assertAlmostEqual(parse_cputime("12:34.56"), 754.56)
        self.assertAlmostEqual(parse_cputime("1:02:03.45"), 3723.45)
        self.assertAlmostEqual(parse_cputime("2-01:00:00"), 2 * 86400 + 3600)


class MacParsingTest(unittest.TestCase):
    def test_ps_keeps_spaces_in_names(self):
        out = (
            "  1     0   0   0  13600   2:43.21 /sbin/launchd\n"
            "512     1 501   0 420000   1:05.00 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome\n"
            "513   512 501  10  90000   0:00.50 claude\n"
            "garbage line\n"
        )
        procs = parse_ps(out)
        self.assertEqual(set(procs), {1, 512, 513})
        self.assertEqual(procs[512]["comm"], "Google Chrome")
        self.assertEqual(procs[512]["rss"], 420000 * 1024)
        self.assertAlmostEqual(procs[512]["cpu_time"], 65.0)
        self.assertEqual(procs[513]["nice"], 10)
        self.assertEqual(procs[513]["ppid"], 512)

    def test_args(self):
        self.assertEqual(parse_args("  42 /usr/bin/node build.js --watch\n x\n"), {42: "/usr/bin/node build.js --watch"})

    def test_vm_stat(self):
        out = (
            "Mach Virtual Memory Statistics: (page size of 16384 bytes)\n"
            "Pages free:                               10000.\n"
            "Pages active:                            200000.\n"
            "Pages inactive:                          150000.\n"
            "Pages speculative:                         5000.\n"
        )
        page, pages = parse_vm_stat(out)
        self.assertEqual(page, 16384)
        self.assertEqual(pages["free"], 10000)
        self.assertEqual(pages["inactive"], 150000)

    def test_swapusage(self):
        total, used = parse_swapusage("total = 2048.00M  used = 1024.50M  free = 1023.50M  (encrypted)")
        self.assertEqual(total, 2048 * 2 ** 20)
        self.assertEqual(used, int(1024.5 * 2 ** 20))
        self.assertEqual(parse_swapusage(""), (0, 0))


if __name__ == "__main__":
    unittest.main()
