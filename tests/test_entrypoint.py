"""Check optional-command dispatch with the package installed without extras."""

import errno
import importlib.util
import os
import pty
import subprocess
import sys
import unittest


class TuiEntryPointTests(unittest.TestCase):
    def invoke(self, *args):
        return subprocess.run([sys.executable, "-m", "ydl", *args], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=5, check=False)

    def test_help_and_argument_errors_do_not_require_a_terminal(self):
        result = self.invoke("tui", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("pre-fills the draft", result.stdout)
        self.assertIn("tui [URL]", self.invoke("--help").stdout)
        self.assertEqual(self.invoke("tui", "one", "two").returncode, 2)
        self.assertEqual(self.invoke().returncode, 2)

    def test_noninteractive_launch_uses_plain_cli_remedy(self):
        result = self.invoke("tui")
        self.assertEqual(result.returncode, 1)
        self.assertIn("run ydl URL", result.stderr)
        self.assertNotIn("\x1b", result.stderr + result.stdout)

    @unittest.skipIf(importlib.util.find_spec("textual"), "Requires the base installation")
    def test_supported_pty_with_textual_absent_shows_install_hint(self):
        master, slave = pty.openpty()
        try:
            with subprocess.Popen([sys.executable, "-m", "ydl", "tui"], stdin=slave, stdout=slave,
                                  stderr=subprocess.PIPE, text=True, env={**os.environ, "TERM": "xterm"}) as child:
                os.close(slave)
                slave = None
                _, error = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 1, error)
                self.assertIn("pipx install '.[tui]'", error)
                self.assertNotIn("\x1b", error)
                with self.assertRaises(OSError) as eof:
                    os.read(master, 1024)
                self.assertEqual(eof.exception.errno, errno.EIO)
        finally:
            os.close(master)
            if slave is not None:
                os.close(slave)
