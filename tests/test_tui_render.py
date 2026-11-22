"""Verify rendered dashboard and modal layout at terminal viewports."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import patch

from textual.widgets import Checkbox, RadioButton, Select, TabbedContent, Tabs

from ydl import config, jobs, runner
from ydl.models import BackendStatus, Decision, Event, Settings
from ydl.tui_dialogs import RunnerPrompt, SettingsPanel
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

    async def test_download_options_remain_visible_and_clickable_after_narrow_resize(self):
        self.disk.save([self.entry("A")])
        app = self.app()
        async with app.run_test(size=(100, 24)) as pilot:
            await self.ready(pilot, app)
            await pilot.pause()
            self.assertTrue(any("Start queue" in line for line in self.rendered_lines(app)))

            await pilot.resize_terminal(100, 35)
            await pilot.pause()
            destination = app.dashboard.query_one("#destination")
            media_options = app.dashboard.query_one("#media-options")
            self.assertGreater(destination.region.y, media_options.region.y)
            self.assertTrue(any("~/Downloads/ydl" in line and "Browse" in line
                                for line in self.rendered_lines(app)))

            quality = app.dashboard.query_one("#quality", Select)
            retain = app.dashboard.query_one("#retain", Checkbox)
            for width in (60, 48):
                await pilot.resize_terminal(width, 20)
                await pilot.pause()
                rendered = "\n".join(self.rendered_lines(app))
                self.assertIn("MP3 quality", rendered)
                self.assertIn("Keep video", rendered)
                await pilot.click(quality)
                self.assertTrue(quality.expanded)
                await pilot.press("escape")
                original = retain.value
                self.assertTrue(await pilot.click(retain))
                self.assertEqual(retain.value, not original)

    async def test_restored_single_video_shows_verified_mp3_before_video_is_complete(self):
        row = replace(self.entry("A", keep_video=True), status="interrupted")
        self.disk.save([row])
        store = jobs.JobStore(self.paths.state)
        item = jobs.make_item(1, "A", "Song", False)
        mp3 = self.output / f"{item.stem}.mp3"
        mp3.write_bytes(b"saved MP3")
        item.outputs["mp3"] = {"name": mp3.name, "sha256": hashlib.sha256(mp3.read_bytes()).hexdigest()}
        item.video_target = f"{item.stem}.mp4"
        settings = Settings("single", "0", True, self.output)
        with store.claim(row.id, new=True):
            store.save(jobs.Job.create(row.id, settings, [item], enqueued_base=row.output))

        app = self.app()
        async with app.run_test(size=(100, 24)) as pilot:
            await self.ready(pilot, app)
            await pilot.pause()
            rendered = "\n".join(self.rendered_lines(app))
            self.assertIn(mp3.name, next(line for line in rendered.splitlines() if "Song" in line))
            self.assertTrue(app.session.selected_retry(row.id))
            self.assertIn("Resume queue", rendered)

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
        async with self.populated_dashboard() as (app, pilot, emit, event):
            queue = app.dashboard.query_one("#queue")
            await pilot.resize_terminal(100, 26)
            await self.wait_for(pilot, lambda: len(queue.columns) == 7)
            self.assertEqual([column.key.value for column in queue.ordered_columns],
                             ["position", "title", "type", "status", "progress", "output", "actions"])
            for width, height in ((150, 38), (220, 55)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                self.assertEqual(app.dashboard.query_one("#destination").region.y,
                                 app.dashboard.query_one("#media-options").region.y)
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
            emit(replace(event, position=100, total=120))
            await pilot.resize_terminal(100, 24)
            await pilot.pause()
            row = next(line for line in self.rendered_lines(app) if "Downloading" in line and "61%" in line)
            self.assertIn("Downloading 100/120", row)
            await pilot.resize_terminal(150, 38)
            await pilot.pause()
            settings = app.dashboard.query_one("#settings")
            self.assertLess(settings.region.y, queue.region.y)
            self.assertTrue(await pilot.click("#settings"))
            await self.wait_for(pilot, lambda: isinstance(app.screen, SettingsPanel) and app.screen.ready)
            self.assertGreater(app.screen.query_one(".dialog").size.width, 72)
            tabbed = app.screen.query_one(TabbedContent)
            rail = tabbed.query_one(Tabs)
            output = app.screen.query_one("#settings-output")
            self.assertLessEqual(rail.region.right, output.region.x)
            rendered = self.rendered_lines(app)
            self.assertIn("Browse", next(line for line in rendered if "Output directory" in line))
            self.assertTrue(await pilot.click("#--content-tab-backend-settings"))
            self.assertEqual(tabbed.active, "backend-settings")
            rail.focus()
            await pilot.press("up")
            self.assertEqual(tabbed.active, "general")
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertLess(rail.region.y, output.region.y)
            self.assertLessEqual(output.region.x, rail.region.right)
            await pilot.press("escape")
            self.assertIs(app.focused, settings)
            await pilot.resize_terminal(150, 38)
            await pilot.pause()
            action_x = sum(column.width + 2 * queue.cell_padding for column in queue.ordered_columns[:-1]) + 1
            self.assertTrue(await pilot.click("#queue", offset=(action_x, 1)))
            self.assertIsInstance(app.screen, RowMenu)
            self.assertTrue(await pilot.click("#details"))
            await self.wait_for(pilot, lambda: isinstance(app.screen, Details) and app.screen.ready)
            self.assertGreater(app.screen.query_one(".dialog").size.width, 72)
            text = self.rendered_lines(app)
            self.assertIn("Job Details — Goa Trance Collection", "\n".join(text))
            progress = app.screen.query_one("#details-progress")
            self.assertLessEqual(app.screen.query_one(Tabs).region.right, progress.region.x)
            speed = next(line for line in text if "Speed" in line)
            eta = next(line for line in text if "ETA" in line and "00:17" in line and "Track progress" not in line)
            self.assertIn("1.9 MiB/s", speed)
            self.assertNotEqual(speed, eta)
            await pilot.press("escape")
            self.assertIs(app.focused, queue)

    async def test_playlist_choices_render_as_distinct_selectable_rows(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            decision = Decision("Download", (("single", "This video"), ("playlist", "Entire playlist")),
                                "single", "scope", "ExampleVideo", "ExamplePlaylist")
            task = app.own(app.decide(decision))
            await self.wait_for(pilot, lambda: isinstance(app.screen, RunnerPrompt) and app.screen.ready)
            text = "\n".join(self.rendered_lines(app))
            self.assertIn("What do you want to download?", text)
            choices = app.screen.query(RadioButton)
            self.assertGreater(choices[1].region.y - choices[0].region.y, 1)
            self.assertTrue(choices[0].value)
            self.assertTrue(await pilot.click(choices[1]))
            await pilot.click("#scope-confirm")
            self.assertEqual(await task, "playlist")

            task = app.own(app.decide(decision))
            await self.wait_for(pilot, lambda: isinstance(app.screen, RunnerPrompt) and app.screen.ready)
            await pilot.press("enter")
            self.assertTrue(task.done())
            self.assertEqual(task.result(), "single")
