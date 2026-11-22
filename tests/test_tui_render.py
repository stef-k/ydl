"""Verify rendered dashboard and modal layout at terminal viewports."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import patch

from ydl import config, jobs, runner
from ydl.models import BackendStatus, Event, Settings
from ydl.tui_dialogs import SettingsPanel
from ydl.tui_jobs import Details, RowMenu

from tui_pilot import PilotCase


class RenderTests(PilotCase):
    @asynccontextmanager
    async def populated_dashboard(self):
        active = self.entry("GoaTrance&list=GoaPlaylist")
        completed = self.entry("NightCity")
        failed = replace(self.entry("Unavailable"), status="failed_before_plan")
        self.disk.save([active, self.entry("Yesterday"), self.entry("Pending&list=GoaVolume2"), completed, failed])
        store = jobs.JobStore(self.paths.state)
        item = jobs.make_item(1, "NightCity", "Night City Mix", False)
        media_file = self.output / f"{item.stem}.mp3"
        media_file.write_bytes(b"saved MP3")
        item.outputs["mp3"] = {"name": media_file.name, "sha256": hashlib.sha256(media_file.read_bytes()).hexdigest()}
        with store.claim(completed.id, new=True):
            store.save(jobs.Job.create(completed.id, Settings("single", "0", False, self.output), [item]))
        config.save(self.paths.config, dict(output=str(self.output)))
        settings = Settings("playlist", "0", False, self.output / "Goa Trance Collection [GoaPlaylist]")
        event = Event("downloading", title="Track 03 — 夜の音楽 " + "海辺の星空 " * 16,
                      position=3, total=24, percent=61, speed=1992294, eta=17)
        stopped = asyncio.Event()

        async def download(*_args, emit, **_kwargs):
            emit(Event("settings", title="Goa Trance Collection", total=24, settings=settings))
            emit(event)
            await stopped.wait()
            raise runner.AttemptStopped(planned=False)

        self.backends.side_effect = lambda *_, emit, **__: emit(
            Event("backend", backend=BackendStatus("Up to date", "2026.01.01")))
        app = self.app()
        with patch.object(runner, "download", side_effect=download) as operation:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(pilot, app)
                await pilot.click("#primary")
                await self.wait_for(pilot, lambda: app.session.events.get(active.id) == event)
                app.dashboard.query_one("#queue").focus()
                await pilot.pause()
                try:
                    yield app, pilot, operation.await_args.kwargs["emit"], event
                finally:
                    app.action_stop()
                    stopped.set()
                    await self.wait_for(pilot, lambda: not app.session.busy)

    @staticmethod
    def rendered_lines(app):
        return [strip.text for strip in app.screen._compositor.render_strips()]

    async def test_rendered_transfer_keeps_metrics_below_wide_title(self):
        async with self.populated_dashboard() as (app, pilot, emit, event):
            lines = self.rendered_lines(app)
            title = next(line for line in lines if "Track 03" in line)
            self.assertIn("Downloading", title)
            self.assertIn("Playlist 3/24", title)
            self.assertIn("…", title)
            progress = next(line for line in lines if "Track progress" in line)
            for metric in ("━", "61%", "1.9 MiB/s", "ETA 00:17"):
                self.assertIn(metric, progress)

            emit(replace(event, percent=None))
            await pilot.pause(0.3)
            progress = next(line for line in self.rendered_lines(app) if "Track progress" in line)
            self.assertIn("Working…", progress)
            self.assertIn("ETA 00:17", progress)
            self.assertNotIn("61%", progress)

    async def test_wide_dashboard_and_modal_controls_after_resize(self):
        async with self.populated_dashboard() as (app, pilot, _, _):
            queue = app.dashboard.query_one("#queue")
            await pilot.resize_terminal(100, 26)
            await self.wait_for(pilot, lambda: len(queue.columns) == 7)
            self.assertEqual([column.key.value for column in queue.ordered_columns],
                             ["position", "title", "type", "status", "progress", "output", "actions"])
            for width, height in ((150, 38), (220, 55)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                lines = self.rendered_lines(app)
                active = next(line for line in lines if "Downloading 3/24" in line)
                complete = next(line for line in lines if "Night City Mix" in line)
                self.assertIn("61%", active)
                self.assertIn("━", active)
                self.assertIn("─", active)
                self.assertIn("Goa Trance Collection [GoaPlaylist]/", active)
                self.assertIn("100%", complete)
                self.assertNotIn("─", complete)
                self.assertIn("Night City Mix [NightCity].mp3", complete)
                for key in ("Video Yesterday", "Video Unavailable"):
                    self.assertNotIn("%", next(line for line in lines if key in line))
                transfer = app.dashboard.query_one("#transfer-panel")
                controls = app.dashboard.query_one("#controls")
                self.assertLessEqual(transfer.region.y - queue.region.bottom, 2)
                self.assertLessEqual(controls.region.y - transfer.region.bottom, 1)
            settings = app.dashboard.query_one("#settings")
            self.assertLess(settings.region.y, queue.region.y)
            self.assertTrue(await pilot.click("#settings"))
            await self.wait_for(pilot, lambda: isinstance(app.screen, SettingsPanel) and app.screen.ready)
            self.assertGreater(app.screen.query_one(".dialog").size.width, 72)
            self.assertIn("Backend", "\n".join(self.rendered_lines(app)))
            await pilot.press("escape")
            self.assertIs(app.focused, settings)
            action_x = sum(column.width + 2 * queue.cell_padding for column in queue.ordered_columns[:-1]) + 1
            self.assertTrue(await pilot.click("#queue", offset=(action_x, 1)))
            self.assertIsInstance(app.screen, RowMenu)
            self.assertTrue(await pilot.click("#details"))
            await self.wait_for(pilot, lambda: isinstance(app.screen, Details) and app.screen.ready)
            self.assertGreater(app.screen.query_one(".dialog").size.width, 72)
            self.assertIn("Job Details — Goa Trance Collection", "\n".join(self.rendered_lines(app)))
            await pilot.press("escape")
            self.assertIs(app.focused, queue)
