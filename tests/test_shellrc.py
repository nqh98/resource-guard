import tempfile
import unittest
from pathlib import Path

from resource_guard import shellrc


class ShellRcTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.rc = Path(self.dir.name) / ".bashrc"
        self.original = 'alias ll="ls -l"\nexport EDITOR=vim\n'
        self.rc.write_text(self.original)

    def tearDown(self):
        self.dir.cleanup()

    def test_install_update_remove_round_trip(self):
        block = shellrc.block("nice -n 10", ["claude", "codex"])
        for _ in range(3):
            shellrc.write(self.rc, block)
        text = self.rc.read_text()
        self.assertEqual(text.count(shellrc.MARK_BEGIN), 1)
        self.assertIn("alias claude='_resource_guard_run claude'", text)
        self.assertTrue(text.startswith(self.original))

        shellrc.write(self.rc, shellrc.block("nice -n 15", ["claude"]))
        self.assertIn("nice -n 15", self.rc.read_text())
        self.assertNotIn("codex", self.rc.read_text())

        shellrc.write(self.rc, None)
        self.assertEqual(self.rc.read_text(), self.original)
        self.assertFalse(shellrc.installed(self.rc))

    def test_backup_keeps_the_original(self):
        shellrc.write(self.rc, shellrc.block("x", ["claude"]))
        shellrc.write(self.rc, shellrc.block("y", ["claude"]))
        self.assertEqual((Path(self.dir.name) / ".bashrc.resource-guard.bak").read_text(), self.original)

    def test_missing_file(self):
        rc = Path(self.dir.name) / ".zshrc"
        shellrc.write(rc, shellrc.block("x", ["claude"]))
        self.assertTrue(shellrc.installed(rc))
        shellrc.write(rc, None)
        self.assertEqual(rc.read_text(), "")


if __name__ == "__main__":
    unittest.main()
