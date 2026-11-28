"""Offline CLI setup and assertions for published media."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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


class CliFixture(unittest.TestCase):
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

    def format_tags(self, path):
        result = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format_tags",
                                          "-of", "json=sv=ignore", str(path)], timeout=10)
        return json.loads(result)["format"].get("tags", {})

    def assert_mp3_tags(self, path, **expected):
        tags = self.format_tags(path)
        self.assertEqual({key: tags[key] for key in ("title", "artist", "album") if key in tags}, expected)
