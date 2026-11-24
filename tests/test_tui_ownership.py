"""Drive an installed TUI in a PTY and kill it with a live staging writer."""

import errno
import fcntl
import json
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from ydl import config
from ydl.tui_queue import Entry, QueueFile
from test_cli import make_media

COMMAND = [sys.executable, "-m", "ydl"]


class TerminalProcess:
    def __init__(self, child, descriptor):
        self.child, self.descriptor = child, descriptor
        self.output = bytearray()

    def read(self):
        if select.select([self.descriptor], [], [], 0.03)[0]:
            try:
                self.output.extend(os.read(self.descriptor, 65536))
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise

    def await_output(self, token):
        try:
            until(lambda: token in self.output or self.child.poll() is not None, self.read)
        except AssertionError as exc:
            raise AssertionError(f"Missing {token!r} in terminal output: {self.output[-1400:]!r}") from exc
        if token not in self.output:
            raise AssertionError(self.output.decode(errors="replace")[-1000:])

    def click_primary(self):
        # The first footer button occupies column 3, row 23 at 80×24.
        os.write(self.descriptor, b"\x1b[<0;3;23M\x1b[<0;3;23m")


def until(condition, pump=lambda: time.sleep(0.02), *, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        pump()
    if not condition():
        raise AssertionError("Terminal operation did not reach the expected state")


@contextmanager
def tty_process(env, cwd):
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        with subprocess.Popen([*COMMAND, "tui"], stdin=slave, stdout=slave, stderr=slave,
                              env=env, cwd=cwd, start_new_session=True) as child:
            os.close(slave)
            slave = None
            try:
                yield TerminalProcess(child, master)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=3)
    finally:
        if slave is not None:
            os.close(slave)
        os.close(master)


class HardKillTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.output = self.root / "output"
        self.paths = config.Paths(self.root / "config/ydl/config.toml", self.root / "data/ydl", self.root / "state/ydl")
        config.save(self.paths.config, asdict(config.Preferences(output=str(self.output), auto_update=False)))
        self.paths.backend.parent.mkdir(parents=True)
        shutil.copyfile(Path(__file__).with_name("fake_ytdlp.py"), self.paths.backend)
        self.paths.backend.chmod(0o700)
        make_media(self.root)
        runtime = self.root / "deno"
        runtime.write_text("#!/bin/sh\nprintf 'deno 2.4.0\\n'\n")
        runtime.chmod(0o700)
        self.env = {**os.environ, "PATH": f"{self.root}:{os.environ['PATH']}", "TERM": "xterm-256color",
                    "XDG_CONFIG_HOME": str(self.root / "config"), "XDG_STATE_HOME": str(self.root / "state"),
                    "XDG_DATA_HOME": str(self.root / "data"), "YDL_TEST_SCENARIO": str(self.root)}
        # Source-tree runs use an absolute module search path across the cwd change.
        if "PYTHONPATH" in self.env:
            self.env["PYTHONPATH"] = os.pathsep.join(os.path.abspath(p) for p in self.env["PYTHONPATH"].split(os.pathsep))
        self.disk = QueueFile(self.paths.state)
        self.first, self.second = [Entry.from_url(f"https://youtu.be/{video}", "auto", "0", False, self.output)
                                   for video in ("first", "second")]
        self.disk.save([self.first, self.second])
        self.scenario = self.root / "scenario.json"
        self.release = self.root / "release"
        self.addCleanup(self.release.touch, exist_ok=True)

    def hold_writer(self):
        staging = self.output / ".ydl-partials" / self.first.id / "1" / "writer.part"
        code = f"""
import os, pathlib, signal, time
root = pathlib.Path({str(self.root)!r})
path = pathlib.Path({str(staging)!r})
os.setsid()
def stop(*_):
    (root / 'cleaning').touch()
    while not (root / 'release').exists():
        with path.open('a') as stream:
            stream.write('x')
        time.sleep(0.01)
    (root / 'exited').touch()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
(root / 'writing').touch()
while True:
    with path.open('a') as stream:
        stream.write('x')
    time.sleep(0.01)
"""
        self.scenario.write_text(json.dumps({"hang": True, "child": code}))

    def test_killed_tui_and_cli_cannot_overtake_the_old_writer(self):
        self.hold_writer()
        other_cwd = self.root / "other-cwd"
        other_cwd.mkdir()
        with tty_process(self.env, self.root) as terminal:
            terminal.await_output(b"Ready")
            terminal.click_primary()
            until(lambda: (self.root / "writing").exists(), terminal.read)
            terminal.child.kill()
            terminal.child.wait(timeout=3)
            until(lambda: (self.root / "cleaning").exists(), terminal.read)
            self.assert_old_owner_blocks(other_cwd)
        self.release.touch()
        until(lambda: (self.root / "exited").exists())
        pid = int((self.root / "child_pid").read_text())
        until(lambda: not Path(f"/proc/{pid}").exists())
        self.scenario.write_text("{}")
        with tty_process(self.env, other_cwd) as restarted:
            restarted.await_output(b"Resume queue")
            until(lambda: b"updates off" in restarted.output, restarted.read)
            self.assertEqual(self.statuses(), ["active", "pending"])
            restarted.click_primary()
            until(lambda: self.statuses() == ["complete", "complete"], restarted.read)
            os.write(restarted.descriptor, b"\x11")
            until(lambda: restarted.child.poll() is not None, restarted.read)
            self.assertEqual(restarted.child.returncode, 0)
        self.assertEqual(len(list(self.output.glob("*.mp3"))), 2)
        calls = [json.loads(line) for line in (self.root / "calls").read_text().splitlines()]
        ids = [call[-1].split("=")[-1] for call in calls]
        self.assertEqual(ids, ["first", "first", "first", "second", "second"])

    def statuses(self):
        return [row.status for row in self.disk.read()]

    def assert_old_owner_blocks(self, cwd):
        with tty_process(self.env, cwd) as contender:
            contender.await_output(b"Queue is busy")
            contender.child.wait(timeout=3)
            self.assertEqual(contender.child.returncode, 1, contender.output)
        retry = subprocess.run([*COMMAND, "retry", self.first.id], env=self.env, cwd=cwd,
                               stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=3)
        self.assertEqual(retry.returncode, 1, retry.stderr)
        self.assertIn("Job already running", retry.stderr)
        self.assertEqual(self.statuses(), ["active", "pending"])
