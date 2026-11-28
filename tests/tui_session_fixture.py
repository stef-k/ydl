"""Offline setup shared by queue session tests."""

import asyncio
import hashlib
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from ydl import backend, config, jobs, media, runner
from ydl.models import Settings
from ydl.tui_queue import Entry, QueueFile
from ydl.tui_session import QueueSession


class SessionCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.paths = config.Paths(self.root / "config/ydl/config.toml", self.root / "data/ydl", self.root / "state/ydl")
        self.disk = QueueFile(self.paths.state)
        self.store = jobs.JobStore(self.paths.state)
        self.output = self.root / "output"
        self.output.mkdir()
        self.prefs = config.Preferences(output=str(self.output), auto_update=False)
        config.save(self.paths.config, asdict(self.prefs))
        self.paths.backend.parent.mkdir(parents=True)
        self.paths.backend.touch(mode=0o700)
        self.backend = self.enterContext(patch.object(backend, "ensure_backend", return_value=None))
        self.version = self.enterContext(patch.object(backend, "backend_version", return_value="2026.01.01"))
        self.decide = None

    def entry(self, name, *, status="pending", keep_video=False):
        row = Entry.from_url(f"https://youtu.be/{name}", "auto", "0", keep_video, self.output)
        return replace(row, status=status)

    def plan(self, row, *tracks, complete=False, scope="single"):
        dest = self.output if scope == "single" else self.output / "Offline playlist [P]"
        dest.mkdir(exist_ok=True)
        items = [jobs.make_item(number, video, video, scope == "playlist") for number, video in enumerate(tracks, 1)]
        plan = jobs.Job.create(row.id, Settings(scope, row.quality, row.keep_video, dest), items,
                               enqueued_base=row.output)
        if complete:
            for item in items:
                self.save_mp3(plan, item)
        with self.store.claim(row.id, new=True):
            self.store.save(plan)
        return plan

    def save_mp3(self, plan, item):
        path = Path(plan.destination) / f"{item.stem}.mp3"
        path.write_bytes(b"example MP3 bytes")
        item.outputs["mp3"] = {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        return path

    def make_session(self):
        session = QueueSession(self.paths, self.disk, lambda **_: None, self.decide)
        self.addAsyncCleanup(session.stop, quitting=True)
        return session

    async def start(self, *rows):
        self.disk.save(rows)
        session = self.make_session()
        await session.open()
        return session

    async def wait(self, predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.01)

    async def run_queue(self, session, mode):
        session.arm(mode)
        self.assertIsNotNone(session.serial)
        await session.serial

    def offline_media(self):
        async def transfer(_command, video_id, staging, keep_video, _progress, _log, *, collect_metadata=False):
            source = staging / ("source.mkv" if keep_video else "source.wav")
            source.write_text(video_id)
            return source, None

        async def convert(_ffmpeg, _ffprobe, source, output, _quality, _candidates, _log):
            output.write_bytes(source.read_bytes())

        deps = runner.Dependencies([], "offline-ffmpeg", "offline-ffprobe")
        self.enterContext(patch.object(runner, "prepare", return_value=deps))
        self.enterContext(patch.object(media, "convert", side_effect=convert))
        self.enterContext(patch.object(media, "verify_output", return_value=None))
        return self.enterContext(patch.object(media, "transfer", side_effect=transfer))
