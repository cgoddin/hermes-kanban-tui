"""Fresh user-bin installation in a disposable HOME; all task data is fictional."""
from contextlib import closing
import hashlib
import os
import sqlite3
import subprocess
import sys
import unittest

from tests.test_kanban import CHILD_ENV, FixtureCase, WRAPPER
from tests.test_pty import PTYChild


@unittest.skipUnless(sys.platform == "linux", "real PTY install smoke currently verified on Linux")
class FreshInstallTests(FixtureCase):
    def setUp(self):
        super().setUp()
        self.user_bin = self.home / ".local/bin"
        self.user_bin.mkdir(parents=True)
        # Pin wrapper's env-python to the interpreter running this suite.
        (self.user_bin / "python3").symlink_to(sys.executable)
        self.env = dict(CHILD_ENV, HOME=str(self.home),
                        PATH=str(self.user_bin) + os.pathsep + CHILD_ENV["PATH"])
        self.link = self.user_bin / "herdr-kanban"
        self.install = 'mkdir -p "$HOME/.local/bin"; ln -s "$1/bin/herdr-kanban" "$HOME/.local/bin/herdr-kanban"'
        self.run_shell(self.install, str(WRAPPER.parent.parent), expected=0)

    def run_shell(self, command, *args, expected):
        result = subprocess.run(["sh", "-eu", "-c", command, "install-test", *args],
                                env=self.env, cwd=self.home, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def test_no_argument_start_details_refresh_quit_and_uninstall(self):
        path = self.db(".hermes/kanban.db", "Fictional ")
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        files_before = sorted(p.name for p in path.parent.iterdir())
        child = PTYChild([str(self.link)], self.home, env=self.env)
        self.addCleanup(child.close)
        child.until(b"Board: Default")
        child.until(b"Fictional triage card")
        child.send(b"j\n")
        child.until(b"DETAIL")
        child.until(b"Ordinary prose")
        child.send(b"r")
        child.until(b"refreshed")
        child.send(b"q")
        self.assertEqual(child.wait(), 0)
        self.assertTrue(child.restored())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), files_before)
        # Repeating the documented install cannot replace any existing command.
        self.run_shell(self.install, str(WRAPPER.parent.parent), expected=1)
        self.assertEqual(self.link.resolve(), WRAPPER)
        self.run_shell('rm "$HOME/.local/bin/herdr-kanban"', expected=0)
        self.assertFalse(self.link.is_symlink())
        self.assertTrue(path.is_file())
        self.assertTrue(WRAPPER.is_file())

    def test_missing_wrong_and_named_board_do_not_fall_back(self):
        result = self.run_shell('herdr-kanban', expected=2)
        self.assertIn("does not exist", result.stderr)
        self.assertFalse((self.home / ".hermes").exists())
        self.db(".hermes/kanban.db", "Default fictional ")
        result = self.run_shell('herdr-kanban --board absent', expected=2)
        self.assertIn("does not exist", result.stderr)
        self.assertFalse((self.home / ".hermes/kanban").exists())
        wrong = self.home / "wrong.db"
        with closing(sqlite3.connect(wrong)) as conn, conn:
            conn.execute("CREATE TABLE unrelated(x)")
        before = wrong.read_bytes()
        result = self.run_shell('herdr-kanban --board "$1"', str(wrong), expected=2)
        self.assertIn("schema", result.stderr)
        self.assertEqual(wrong.read_bytes(), before)
        path = self.db(".hermes/kanban/boards/studio/kanban.db", "Named fictional ")
        current = path.parents[2] / "current"
        current.write_text("studio\n")
        child = PTYChild([str(self.link)], self.home, env=self.env)
        self.addCleanup(child.close)
        child.until(b"Board: studio")
        child.until(b"Named fictional triage card")
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("UPDATE tasks SET title='Fictional external refresh' WHERE id='triage-id'")
        after_writer = path.read_bytes()
        child.send(b"r")
        child.until(b"Fictional external refresh")
        child.send(b"q")
        self.assertEqual(child.wait(), 0)
        self.assertTrue(child.restored())
        self.assertEqual(path.read_bytes(), after_writer)


if __name__ == "__main__":
    unittest.main()
