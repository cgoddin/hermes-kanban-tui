"""Real finite PTY child tests, only against temporary SQLite fixtures."""

from contextlib import closing
import errno
import fcntl
import os
import pty
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import termios
import time
import unittest

from tests.test_kanban import CHILD_ENV, FixtureCase, ROOT, WRAPPER


class PTYChild:
    def __init__(self, command, cwd, size=(24, 100), term="xterm-256color", env=None):
        self.master, self.slave = pty.openpty()
        self.output = bytearray()
        self.process = None
        self.original = termios.tcgetattr(self.slave)
        self.resize(*size, notify=False)
        env = dict(CHILD_ENV if env is None else env, TERM=term)
        try:
            self.process = subprocess.Popen(
                command, cwd=cwd, env=env, stdin=self.slave, stdout=self.slave, stderr=self.slave,
                start_new_session=True, close_fds=True,
            )
        except BaseException:
            os.close(self.master)
            os.close(self.slave)
            raise

    def resize(self, rows, cols, notify=True):
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        if notify and self.process.poll() is None:
            self.process.send_signal(signal.SIGWINCH)

    def drain(self, duration=0.15):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], max(0, deadline - time.monotonic()))[0]:
                try:
                    data = os.read(self.master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise
                if not data:
                    break
                self.output.extend(data)
        return bytes(self.output)

    def until(self, expected, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.drain(0.05)
            if expected in self.output:
                return
            if self.process.poll() is not None:
                break
        raise AssertionError(f"PTY output missing {expected!r}: {bytes(self.output)!r}")

    def send(self, keys):
        os.write(self.master, keys)
        self.drain()

    def wait(self, timeout=5):
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            self.drain(0.05)
        if self.process.poll() is None:
            raise AssertionError("Viewer did not exit in bounded time")
        self.drain()
        return self.process.returncode

    def restored(self):
        return termios.tcgetattr(self.slave) == self.original

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.wait(2)
            except AssertionError:
                self.process.kill()
                self.process.wait(timeout=2)
        os.close(self.master)
        os.close(self.slave)


@unittest.skipUnless(sys.platform == "linux", "PTY resize/termios tests require Linux")
class ViewerPTYTests(FixtureCase):
    def child(self, path, size=(24, 100), term="xterm-256color", command=None):
        command = command or [str(WRAPPER), "--hermes-home", str(self.home), "--project", "studio", "--board", str(path)]
        child = PTYChild(command, self.home, size, term)
        self.addCleanup(child.close)
        return child

    def board(self):
        return self.db("kanban/boards/studio/kanban.db")

    def test_navigation_detail_refresh_narrow_resize_and_q(self):
        path = self.board()
        before = path.read_bytes()
        child = self.child(path)
        child.until(b"Project: studio")
        child.until(b"Board: studio")
        child.until(b"triage card")
        child.send(b"j\n")  # select todo and open detail
        child.until(b"DETAIL")
        child.until(b"Ordinary prose")
        child.until(b"todo-id")
        child.send(b"G")
        child.until(b"comment prose")
        self.assertNotIn(b"comment-secret", child.output)
        self.assertNotIn(b"never-display-this", child.output)
        self.assertNotIn(b"run-secret", child.output)
        self.assertNotIn(b"never-selected", child.output)
        child.resize(5, 18)
        child.send(b"Gk\x1b[6~")
        self.assertIsNone(child.process.poll())
        child.resize(24, 100)
        child.drain(0.3)
        child.send(b"\x1b")
        child.drain(1.2)  # Escape disambiguation in curses
        child.send(b"lkhjr\x1bOB\x1bOA")  # group, row, arrow navigation and refresh
        child.resize(5, 18)
        child.drain(0.3)
        self.assertIsNone(child.process.poll())
        child.resize(2, 8)
        child.drain(0.3)
        self.assertIsNone(child.process.poll())
        child.resize(1, 1)
        child.send(b"r")
        child.resize(24, 100)
        child.drain(0.3)
        child.send(b"q")
        self.assertEqual(child.wait(), 0)
        self.assertTrue(child.restored(), "termios not restored after normal exit")
        self.assertEqual(path.read_bytes(), before)
        self.assertNotIn(b"Traceback", child.output)

    def test_external_refresh_keeps_selected_card_and_detail(self):
        path = self.board()
        child = self.child(path)
        child.until(b"triage card")
        child.send(b"j\n")
        child.until(b"DETAIL")
        with closing(sqlite3.connect(path)) as writer, writer:
            writer.execute("UPDATE tasks SET title='retained refresh title', status='review' WHERE id='todo-id'")
        child.send(b"r")
        child.until(b"retained refresh title")
        child.send(b"q")
        self.assertEqual(child.wait(), 0)
        self.assertTrue(child.restored())

    def test_automatic_refresh_and_hostile_title(self):
        path = self.board()
        child = self.child(path)
        child.until(b"triage card")
        with closing(sqlite3.connect(path)) as writer, writer:
            writer.execute("UPDATE tasks SET title=? WHERE id='triage-id'",
                           ("automatic refreshed title\x1b]52;c;clipboard-secret\x07\u202e API_KEY=title-secret",))
        child.until(b"automatic refreshed title", timeout=7)
        self.assertNotIn(b"clipboard-secret", child.output)
        self.assertNotIn(b"title-secret", child.output)
        self.assertNotIn("\u202e".encode(), child.output)
        child.send(b"q")
        self.assertEqual(child.wait(), 0)
        self.assertTrue(child.restored())

    def test_q_exits_even_at_tiny_size(self):
        child = self.child(self.board(), size=(2, 8))
        child.drain(0.5)
        child.send(b"q")
        self.assertEqual(child.wait(), 0)
        self.assertTrue(child.restored())

    def test_sigterm_restores_terminal(self):
        child = self.child(self.board())
        child.until(b"triage card")
        child.process.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(), 130)
        self.assertTrue(child.restored(), "termios not restored after SIGTERM")
        self.assertNotIn(b"Traceback", child.output)

    def test_sigint_restores_terminal(self):
        child = self.child(self.board())
        child.until(b"triage card")
        child.process.send_signal(signal.SIGINT)
        self.assertEqual(child.wait(), 130)
        self.assertTrue(child.restored(), "termios not restored after SIGINT")

    def test_sighup_restores_terminal(self):
        child = self.child(self.board())
        child.until(b"triage card")
        child.process.send_signal(signal.SIGHUP)
        self.assertEqual(child.wait(), 130)
        self.assertTrue(child.restored(), "termios not restored after SIGHUP")

    def test_terminal_setup_failure_is_safe(self):
        child = self.child(self.board(), term="herdr-test-nonexistent-terminal")
        self.assertEqual(child.wait(), 2)
        self.assertTrue(child.restored())
        self.assertIn(b"viewer failed", child.output)
        self.assertNotIn(b"Traceback", child.output)

    def test_exception_after_raw_mode_restores_terminal_and_redacts_error(self):
        path = self.board()
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); from herdr_workstation import kanban; "
            "exec(\"def fail(*args):\\n raise RuntimeError('TOKEN=exception-secret\\\\x1b[31m')\"); "
            "kanban.run_viewer=fail; sys.exit(kanban.main(['--board',sys.argv[2],'--hermes-home',sys.argv[3]]))"
        )
        child = self.child(path, command=[sys.executable, "-B", "-c", code, str(ROOT), str(path), str(self.home)])
        self.assertEqual(child.wait(), 2)
        self.assertTrue(child.restored())
        self.assertIn(b"REDACTED", child.output)
        self.assertNotIn(b"exception-secret", child.output)
        self.assertNotIn(b"Traceback", child.output)


if __name__ == "__main__":
    unittest.main()
