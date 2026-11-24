"""Shared setup for the installed Textual Pilot tests."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ydl import backend, config
from ydl.tui import YdlApp
from ydl.tui_dialogs import AddURLs
from ydl.tui_queue import Entry, QueueFile


URL = "https://www.youtube.com/watch?v="


class PilotCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.paths = config.Paths(self.root / "config.toml", self.root / "data", self.root / "state")
        self.disk = QueueFile(self.paths.state)
        self.output = self.root / "output"
        self.output.mkdir()
        self.paths.backend.parent.mkdir(parents=True)
        self.paths.backend.touch(mode=0o700)
        self.backends = self.enterContext(patch.object(backend, "ensure_backend", return_value=None))
        self.versions = self.enterContext(patch.object(backend, "backend_version", return_value="2026.01.01"))

    def entry(self, name, **overrides):
        values = dict(scope="auto", quality="0", keep_video=False, output=self.output)
        return Entry.from_url(URL + name, **{**values, **overrides})

    def app(self, prefill=None):
        return YdlApp(self.paths, prefill, self.disk)

    @staticmethod
    def rendered_lines(app):
        return [strip.text for strip in app.screen._compositor.render_strips()]

    def screen_text(self, app):
        return "\n".join(self.rendered_lines(app))

    async def wait_for(self, pilot, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await pilot.pause(0.02)

    async def ready(self, pilot, app):
        await self.wait_for(pilot, lambda: app.session.index.runnable)

    async def submit(self, pilot):
        self.assertTrue(await pilot.click("#add-urls"))
        self.assertIsInstance(pilot.app.screen, AddURLs)
        self.assertTrue(await pilot.click("#submit-add"))
