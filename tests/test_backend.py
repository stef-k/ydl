"""Verify frontend-visible updater behavior with deterministic releases."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ydl import backend
from ydl.config import Preferences
from ydl.models import YdlError
from ydl.process import Log


def package(version):
    return f"print({version!r})\n".encode()


class UpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bin" / "yt-dlp"
        self.events = []
        self.log = Log()
        self.prefs = Preferences(channel="stable", auto_update=False)

    async def update(self, payload, *, tag, check=False, explicit=True):
        release = backend.Release("yt-dlp/yt-dlp", tag)
        manifest = f"{hashlib.sha256(payload).hexdigest()}  yt-dlp\n".encode()
        transfer = AsyncMock(side_effect=[manifest, payload])
        with patch.object(backend, "latest", return_value=release) as latest:
            with patch.object(backend, "fetch", transfer):
                report = await backend.ensure_backend(self.path, self.prefs, check=check, explicit=explicit,
                                                      emit=self.events.append, log=self.log)
        latest.assert_awaited_once_with("yt-dlp/yt-dlp", self.log)
        return report, transfer.await_count

    async def test_check_is_read_only_and_channel_switch_can_downgrade(self):
        report, calls = await self.update(package("2026.11.21"), tag="2026.11.21", check=True)
        self.assertIn("missing | stable: 2026.11.21 | installation available", report)
        self.assertEqual(calls, 1)
        self.assertFalse(self.path.parent.exists())
        future = package("2026.11.21")
        await self.update(future, tag="2026.11.21", explicit=False)
        self.assertEqual(self.path.read_bytes(), future)
        older = package("2026.10.26")
        report, calls = await self.update(older, tag="2026.10.26", check=True)
        self.assertIn("replacement available", report)
        self.assertEqual(calls, 1)
        self.assertEqual(self.path.read_bytes(), future)
        self.assertFalse(self.path.with_suffix(".previous").exists())
        await self.update(older, tag="2026.10.26")
        self.assertEqual(self.path.read_bytes(), older)
        self.assertEqual(self.path.with_suffix(".previous").read_bytes(), future)
        report, calls = await self.update(older, tag="2026.10.26")
        self.assertIn("up to date", report)
        self.assertEqual(calls, 1)

    async def test_bad_automatic_update_uses_previous_backend(self):
        original = package("2026.10.26")
        await self.update(original, tag="2026.10.26")
        self.prefs = Preferences(channel="stable")
        report, _ = await self.update(package("2026.11.10"), tag="2026.11.11", explicit=False)
        self.assertIsNone(report)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertTrue(any("Using 2026.10.26" in event.message for event in self.events))
        status = [event.backend for event in self.events if event.backend][-1]
        self.assertEqual((status.state, status.version), ("Ready · using installed version", "2026.10.26"))
        with self.assertRaisesRegex(YdlError, "does not report the selected release version"):
            await self.update(package("2026.11.10"), tag="2026.11.11")
        self.assertEqual(self.path.read_bytes(), original)

    async def test_checksum_mismatch_cannot_install_a_backend(self):
        release = backend.Release("yt-dlp/yt-dlp", "2026.10.26")
        with patch.object(backend, "latest", return_value=release):
            with patch.object(backend, "fetch", side_effect=[b"0" * 64 + b"  yt-dlp\n", package(release.tag)]):
                with self.assertRaisesRegex(YdlError, "SHA-256 does not match"):
                    await backend.ensure_backend(self.path, self.prefs, explicit=True,
                                                 emit=self.events.append, log=self.log)
        self.assertFalse(self.path.exists())
