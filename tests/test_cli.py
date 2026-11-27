"""Exercise CLI workflows through the package entry point with offline media."""

import errno
import fcntl
import json
import os
import pty
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

from ydl.media import parse_url, playlist_size

VIDEO = "https://www.youtube.com/watch?v=testVideo01"
MIXED = VIDEO + "&list=testPlaylist01"
PLAYLIST = "https://www.youtube.com/playlist?list=testPlaylist01"
ENTRY = {"id": "testVideo01", "title": "Sample song"}
COMMAND = [sys.executable, "-m", "ydl"]


def make_media(folder):
    common = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
              "-i", "sine=frequency=440:duration=0.15"]
    subprocess.run([*common, str(folder / "source.wav")], check=True)
    subprocess.run([*common, "-f", "lavfi", "-i", "color=size=32x32:duration=0.15",
                    "-c:v", "mpeg4", str(folder / "source.mkv")], check=True)


class AddressTests(unittest.TestCase):
    def test_context_and_exact_hostname(self):
        self.assertEqual(parse_url("https://youtu.be/testVideo01?list=P1&t=42").url("single"), VIDEO)
        self.assertEqual(parse_url("https://music.youtube.com/shorts/aB_9").video, "aB_9")
        for url in ("https://youtube.com.example.org/watch?v=x", "https://user@youtube.com/watch?v=x",
                    "https://youtube.com/watch?v=a&v=b"):
            with self.subTest(url=url), self.assertRaisesRegex(Exception, "YouTube"):
                parse_url(url)

    def test_playlist_confirmation_boundary(self):
        self.assertEqual(playlist_size({"entries": [None] * 50}), (50, False))
        self.assertEqual(playlist_size({"entries": [None] * 51}), (None, True))
        self.assertEqual(playlist_size({"playlist_count": 90}), (90, True))
        self.assertEqual(playlist_size({"is_incomplete": True}), (None, None))


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg and ffprobe required")
class CliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.fixtures.cleanup)
        make_media(Path(cls.fixtures.name))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.output = self.root / "downloads"
        self.scenario = self.root / "scenario.json"
        private_bin = self.root / "data" / "ydl" / "bin"
        private_bin.mkdir(parents=True)
        self.backend = private_bin / "yt-dlp"
        shutil.copyfile(Path(__file__).with_name("fake_ytdlp.py"), self.backend)
        self.backend.chmod(0o700)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.deno = self.bin / "deno"
        self.deno.write_text("#!/bin/sh\nprintf 'deno 2.4.0\\n'\n")
        self.deno.chmod(0o700)
        for name in ("source.wav", "source.mkv"):
            shutil.copyfile(Path(self.fixtures.name) / name, self.root / name)
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                    "XDG_CONFIG_HOME": str(self.root / "config"),
                    "XDG_DATA_HOME": str(self.root / "data"),
                    "XDG_STATE_HOME": str(self.root / "state"),
                    "YDL_TEST_SCENARIO": str(self.root), "TERM": "xterm"}
        self.write_scenario(entries=[ENTRY])

    def write_scenario(self, **kwargs):
        self.scenario.write_text(json.dumps(kwargs))

    def invoke(self, *args):
        return subprocess.run([*COMMAND, *args], env=self.env, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=20, check=False)

    def fetch(self, url=VIDEO, *flags):
        return self.invoke(url, "--no-update", "--output", str(self.output), *flags)

    def check_code(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def calls(self):
        path = self.root / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def job_id(self):
        directory = self.root / "state" / "ydl" / "jobs"
        return next(directory.glob("*.json")).stem

    def test_single_audio_default_publishes_real_mp3_and_preserves_collision(self):
        self.write_scenario(title="Sample song [OFFICIAL VIDEO]")
        self.check_code(self.fetch(MIXED, "--yes"), 0)
        mp3 = self.output / "Sample song [OFFICIAL VIDEO].mp3"
        self.assertGreater(mp3.stat().st_size, 500)
        with mp3.open("rb") as stream:
            contents = stream.read()
        calls = self.calls()
        self.assertEqual([call[-1] for call in calls], [VIDEO, VIDEO])
        self.assertIn("bestaudio/best", calls[-1])
        self.assertEqual(list(self.output.iterdir()), [mp3])
        job = self.job_id()
        self.backend.unlink()
        self.check_code(self.invoke("retry", job), 0)
        self.assertEqual(self.calls(), calls)
        shutil.copyfile(Path(__file__).with_name("fake_ytdlp.py"), self.backend)
        self.backend.chmod(0o700)
        duplicate = self.fetch(VIDEO.replace("testVideo01", "testVideo02"), "--yes")
        self.check_code(duplicate, 1)
        self.assertIn("Output already exists", duplicate.stderr)
        self.assertEqual(mp3.read_bytes(), contents)

    def test_keep_video_and_saved_configuration(self):
        self.check_code(self.invoke("config", "set", "keep_video", "true"), 0)
        self.check_code(self.invoke("config", "set", "quality", "192k"), 0)
        self.check_code(self.fetch(VIDEO, "--yes"), 0)
        self.assertEqual({file.suffix for file in self.output.iterdir()}, {".mp3", ".mkv"})
        self.assertIn("bv*+ba/b", self.calls()[-1])
        shown = self.invoke("config", "show")
        self.assertIn('quality = "192K"', shown.stdout)
        self.assertNotIn(str(self.output), shown.stdout)

    def test_retry_preserves_verified_mp3_when_video_publication_was_interrupted(self):
        self.check_code(self.invoke("config", "set", "keep_video", "true"), 0)
        preferences = self.root / "config" / "ydl" / "config.toml"
        preferences.write_text(preferences.read_text().replace("auto_update = true", "auto_update = false"))
        self.check_code(self.fetch(VIDEO, "--yes"), 0)
        video = self.output / "Sample song.mkv"
        mp3 = self.output / "Sample song.mp3"
        saved = mp3.stat()
        # Keep the verification receipts and model interruption between the two publications.
        video.unlink()
        directory = self.root / "state" / "ydl" / "jobs"
        record = directory / f"{self.job_id()}.json"
        state = json.loads(record.read_text())
        state["complete"] = False
        record.write_text(json.dumps(state))
        self.check_code(self.invoke("retry"), 0)
        self.assertTrue(video.is_file())
        self.assertEqual(mp3.stat().st_ino, saved.st_ino)
        self.assertEqual(mp3.stat().st_mtime_ns, saved.st_mtime_ns)

    def test_confirmation_precedes_media(self):
        self.write_scenario(entries=[ENTRY] * 51)
        selected = self.fetch(MIXED, "--playlist")
        self.check_code(selected, 2)
        self.assertIn("Pass --yes", selected.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("--playlist-end", self.calls()[0])
        self.assertIn("51", self.calls()[0])
        self.assertFalse(self.output.exists())

    def test_yes_confirms_unknown_size_after_explicit_playlist_selection(self):
        second = {"id": "testVideo02", "title": "Second"}
        self.write_scenario(entries=[ENTRY, second], probe={"entries": None, "is_incomplete": True})
        self.check_code(self.fetch(MIXED, "--playlist"), 2)
        self.check_code(self.fetch(MIXED, "--playlist", "--yes"), 0)
        self.assertEqual(len(list(self.output.rglob("*.mp3"))), 2)
        self.assertEqual(len(self.calls()), 5)

    def test_playlists_with_same_title_get_separate_folders(self):
        self.check_code(self.fetch(PLAYLIST, "--yes"), 0)
        first = self.output / "Test playlist" / "001 - Sample song.mp3"
        original = first.stat()
        occupied = self.output / "Test playlist (2)"
        occupied.write_text("Keep this file")
        dangling = self.output / "Test playlist (3)"
        dangling.symlink_to(self.root / "missing")

        other_playlist = PLAYLIST.replace("testPlaylist01", "testPlaylist02")
        self.check_code(self.fetch(other_playlist, "--yes"), 0)

        self.assertTrue((self.output / "Test playlist (4)" / first.name).is_file())
        self.assertEqual(first.stat(), original)
        self.assertEqual(occupied.read_text(), "Keep this file")
        self.assertEqual(dangling.readlink(), self.root / "missing")
        self.assertFalse(dangling.exists())

    def test_retry_uses_saved_playlist_order_and_skips_completed_items(self):
        second = {"id": "testVideo02", "title": "Second"}
        third = {"id": "testVideo03", "title": "Third"}
        self.write_scenario(entries=[ENTRY, second, third], fail=[second["id"]])
        self.check_code(self.invoke("config", "set", "channel", "stable"), 0)
        preferences = self.root / "config" / "ydl" / "config.toml"
        preferences.write_text(preferences.read_text().replace("auto_update = true", "auto_update = false"))
        occupied = self.output / "Test playlist"
        occupied.mkdir(parents=True)
        blocked = self.fetch(PLAYLIST, "--yes")
        self.check_code(blocked, 1)
        self.assertIn("2 MP3 files, 1 failed", blocked.stderr)
        self.assertIn("private", blocked.stderr)
        folder = self.output / "Test playlist (2)"
        first = folder / "001 - Sample song.mp3"
        old = first.stat().st_mtime_ns
        self.write_scenario(entries=[third, ENTRY])
        self.check_code(self.invoke("retry"), 0)
        self.assertEqual(first.stat().st_mtime_ns, old)
        self.assertTrue((folder / "002 - Second.mp3").is_file())
        self.assertEqual(list(occupied.iterdir()), [])
        self.assertTrue(self.calls()[-1][-1].endswith(second["id"]))
        self.assertEqual(len(self.calls()), 5)

    def test_invalid_arguments_and_missing_private_backend(self):
        self.check_code(self.fetch(VIDEO, "--single", "--playlist"), 2)
        self.check_code(self.fetch(PLAYLIST, "--single"), 2)
        self.check_code(self.fetch(VIDEO, "--quality", "high"), 2)
        self.backend.unlink()
        result = self.fetch(VIDEO, "--yes")
        self.check_code(result, 1)
        self.assertIn("Run ydl update", result.stderr)
        self.assertFalse(self.backend.exists())

    def test_yes_requires_separate_runtime_consent(self):
        self.deno.unlink()
        for name in ("ffmpeg", "ffprobe"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.env["PATH"] = str(self.bin)
        result = self.fetch(VIDEO, "--yes")
        self.check_code(result, 1)
        self.assertIn("Deno 2.3+ or Node 22+", result.stderr)
        self.assertFalse(self.calls())

    def test_real_terminal_uses_two_rows_and_plain_mode_omits_updates(self):
        code, result = self.capture_terminal()
        self.assertEqual(code, 0, result)
        self.assertEqual(result.count("Single video | MP3 VBR 0"), 1)
        self.assertIn("Downloading Sample song", result)
        self.assertIn("Converting Sample song", result)
        self.assertIn("68%", result)
        self.assertIn("\r\x1b[2K", result)
        self.assertEqual(result.count("\n"), 2)
        plain = self.invoke("retry", self.job_id())
        self.check_code(plain, 0)
        self.assertNotIn("\x1b", plain.stderr)
        self.assertNotIn("Keep video off", plain.stderr)

    def capture_terminal(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        args = [*COMMAND, VIDEO, "--no-update", "--yes", "--output", str(self.output)]
        with subprocess.Popen(args, env=self.env, stdout=subprocess.DEVNULL, stderr=slave,
                              stdin=subprocess.DEVNULL) as child:
            os.close(slave)
            try:
                data = bytearray()
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and select.select([master], [], [], 1)[0]:
                    try:
                        data.extend(os.read(master, 65536))
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                        break
                return child.wait(timeout=2), data.decode(errors="replace")
            finally:
                os.close(master)

    def test_ctrl_c_stops_descendant_and_keeps_partial(self):
        # The descendant records its termination before returning from SIGTERM.
        child_code = ("import pathlib, signal, time\n"
                      "def stopped(*_):\n"
                      "    pathlib.Path('stopped').write_text('yes')\n"
                      "    raise SystemExit(0)\n"
                      "signal.signal(signal.SIGTERM, stopped)\n"
                      "pathlib.Path('ready').touch()\n"
                      "time.sleep(60)\n").replace("'stopped'", repr(str(self.root / "stopped"))).replace(
                          "'ready'", repr(str(self.root / "ready")))
        self.write_scenario(entries=[ENTRY], hang=True, child=child_code)
        args = [*COMMAND, VIDEO, "--no-update", "--yes", "--output", str(self.output)]
        with subprocess.Popen(args, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, text=True) as child:
            try:
                deadline = time.monotonic() + 7
                while not (self.root / "ready").exists() and child.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue((self.root / "ready").exists())
                child.send_signal(signal.SIGINT)
                _, errors = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 130, errors)
                self.assertRegex(errors, r"Resume with ydl retry [a-f0-9]{32}")
                self.assertTrue((self.root / "stopped").exists())
                self.assertTrue(any(self.output.rglob("*.part")))
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()
