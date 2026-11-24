"""Verify rendered dashboard and modal layout at terminal viewports."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import patch

from textual.events import MouseDown, MouseScrollDown, MouseScrollUp, MouseUp
from textual.widgets import Checkbox, RadioButton, Select, TabbedContent, Tabs

from ydl import config, jobs, runner
from ydl.models import BackendStatus, Decision, Event, Settings
from ydl.tui_dialogs import AddURLs, Browse, HelpPanel, RunnerPrompt, SettingsPanel
from ydl.tui_jobs import Details, RowMenu

from tui_pilot import PilotCase


class RenderTests(PilotCase):
    @asynccontextmanager
    async def populated_dashboard(self, extra_rows=()):
        active = self.entry("GoaTrance&list=GoaPlaylist")
        completed = self.entry("NightCity")
        failed = replace(self.entry("Unavailable"), status="failed_before_plan")
        interrupted = replace(self.entry("Volume2&list=GoaVolume2"), status="interrupted")
        self.disk.save([active, self.entry("Yesterday"), interrupted, completed, failed, *extra_rows])
        store = jobs.JobStore(self.paths.state)
        settings = Settings("playlist", "0", False, self.output / "Goa Trance Vol. 2")
        track = jobs.make_item(1, "Volume2", "First track", True)
        with store.claim(interrupted.id, new=True):
            store.save(jobs.Job.create(interrupted.id, settings, [track], enqueued_base=interrupted.output))
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
            summary = app.dashboard.query_one("#queue-summary")
            header = " ".join(lines[summary.region.y:summary.region.bottom])
            for value in ("5 URLs", "Active 3/24", "1 Pending", "1 Interrupted", "1 Complete", "1 Failed"):
                self.assertIn(value, header)
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

    async def test_keyboard_selection_stays_visible_with_a_wrapped_queue_summary(self):
        async with self.populated_dashboard() as (app, pilot, _, _):
            queue = app.dashboard.query_one("#queue")
            await pilot.resize_terminal(100, 24)
            await pilot.pause()
            await pilot.press("ctrl+end")
            await pilot.wait_for_animation()
            await pilot.pause()
            for width, height in ((80, 20), (40, 24)):
                with self.subTest(width=width, height=height):
                    await pilot.resize_terminal(width, height)
                    await pilot.pause()
                    self.assertIs(app.focused, queue)
                    self.assertIn("× Video", self.screen_text(app))
                    await pilot.press("ctrl+home")
                    await pilot.wait_for_animation()
                    await pilot.pause()
                    rendered = self.screen_text(app)
                    self.assertIn("▶ Goa Tr", rendered)
                    self.assertIn("Stop queue", rendered)
                    await pilot.press("ctrl+end")
                    await pilot.wait_for_animation()
                    await pilot.pause()
                    self.assertIn("× Video", self.screen_text(app))

    async def test_height_only_resize_keeps_the_focused_queue_row_visible(self):
        entries = [self.entry(str(number)) for number in range(30)]
        self.disk.save(entries)
        app = self.app()
        async with app.run_test(size=(80, 20)) as pilot:
            await self.ready(pilot, app)
            queue = app.dashboard.query_one("#queue")
            queue.focus()
            await pilot.press("ctrl+end")
            await pilot.wait_for_animation()
            self.assertIn("○ Video 29", self.screen_text(app))

            await pilot.resize_terminal(80, 18)
            await pilot.wait_for_animation()
            await pilot.pause()
            self.assertIs(app.focused, queue)
            self.assertEqual(app.selected, entries[-1].id)
            self.assertIn("○ Video 29", self.screen_text(app))

    async def test_double_click_opens_the_clicked_row_in_a_partially_visible_queue(self):
        async with self.populated_dashboard() as (app, pilot, _, _):
            await pilot.resize_terminal(100, 24)
            await pilot.press("ctrl+end")
            await pilot.resize_terminal(80, 20)
            await pilot.pause()
            summary = app.dashboard.query_one("#queue-summary")
            await pilot._post_mouse_events([MouseScrollUp], summary, offset=(1, 0))
            await pilot.wait_for_animation()
            await pilot.pause()

            row_y = next(y for y, line in enumerate(self.rendered_lines(app)) if "✓ Night City Mix" in line)
            completed = app.session.index.rows[3].id
            self.assertTrue(await pilot.double_click(offset=(12, row_y)))
            self.assertIsInstance(app.screen, Details)
            self.assertEqual(app.screen.row_id, completed)

    @staticmethod
    async def send_mouse(pilot, x, y, *events):
        # Use the app's mouse dispatch so Textual calculates the click chain.
        for event in events:
            pilot.app.post_message(event(None, x, y, 0, 0, 1, False, False, False))
        await pilot.pause()

    async def test_live_summary_wrap_cannot_complete_a_double_click_on_another_row(self):
        async with self.populated_dashboard() as (app, pilot, emit, event):
            await pilot.resize_terminal(84, 24)
            emit(replace(event, position=9))
            await self.wait_for(pilot, lambda: "Active 9/24" in self.screen_text(app))
            row_y = next(y for y, line in enumerate(self.rendered_lines(app)) if "✓ Night City Mix" in line)

            with patch.object(app, "CLICK_CHAIN_TIME_THRESHOLD", 5):
                await self.send_mouse(pilot, 12, row_y, MouseDown, MouseUp)
                self.assertEqual(app.selected, app.session.index.rows[3].id)
                emit(replace(event, position=10))
                await self.wait_for(pilot, lambda: "↻ Goa Trance Vol. 2" in self.rendered_lines(app)[row_y])
                await self.send_mouse(pilot, 12, row_y, MouseDown, MouseUp)
                self.assertIs(app.screen, app.dashboard)
                self.assertEqual(app.selected, app.session.index.rows[2].id)

    async def test_live_summary_wrap_invalidates_a_press_over_a_different_row(self):
        async with self.populated_dashboard() as (app, pilot, emit, event):
            await pilot.resize_terminal(84, 24)
            emit(replace(event, position=9))
            await self.wait_for(pilot, lambda: "Active 9/24" in self.screen_text(app))
            row_y = next(y for y, line in enumerate(self.rendered_lines(app)) if "✓ Night City Mix" in line)
            selected = app.selected

            with patch.object(app, "CLICK_CHAIN_TIME_THRESHOLD", 5):
                await self.send_mouse(pilot, 12, row_y, MouseDown)
                emit(replace(event, position=10))
                await self.wait_for(pilot, lambda: "↻ Goa Trance Vol. 2" in self.rendered_lines(app)[row_y])
                await self.send_mouse(pilot, 12, row_y, MouseUp)
                self.assertEqual(app.selected, selected)
                await self.send_mouse(pilot, 12, row_y, MouseDown, MouseUp)
                self.assertIs(app.screen, app.dashboard)
                self.assertEqual(app.selected, app.session.index.rows[2].id)

    async def test_help_click_cannot_complete_a_queue_double_click(self):
        entries = [self.entry(str(number)) for number in range(10)]
        self.disk.save(entries)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            row_y = next(y for y, line in enumerate(self.rendered_lines(app)) if "○ Video 2 " in line)
            with patch.object(app, "CLICK_CHAIN_TIME_THRESHOLD", 5):
                await self.send_mouse(pilot, 16, row_y, MouseDown, MouseUp)
                self.assertEqual(app.selected, entries[2].id)
                await pilot.press("question_mark")
                await self.wait_for(pilot, lambda: isinstance(app.screen, HelpPanel) and app.screen.ready)
                await self.send_mouse(pilot, 16, row_y, MouseDown, MouseUp)
                self.assertIsInstance(app.screen, HelpPanel)
                await pilot.press("escape")
                self.assertIs(app.screen, app.dashboard)
                await self.send_mouse(pilot, 16, row_y, MouseDown, MouseUp)
                self.assertIs(app.screen, app.dashboard)
                self.assertEqual(app.selected, entries[2].id)

    async def test_selection_stays_visible_when_live_progress_wraps_the_summary(self):
        async with self.populated_dashboard() as (app, pilot, emit, event):
            await pilot.resize_terminal(84, 24)
            emit(replace(event, position=9))
            await self.wait_for(pilot, lambda: "Active 9/24" in self.screen_text(app))
            await pilot.press("ctrl+end")
            await pilot.wait_for_animation()
            await pilot.pause()
            selected = app.selected
            self.assertIn("× Video Unavailable", self.screen_text(app))

            emit(replace(event, position=10))
            summary = app.dashboard.query_one("#queue-summary")
            await self.wait_for(pilot, lambda: summary.region.height == 2)
            await pilot.pause()
            self.assertEqual(app.selected, selected)
            self.assertIn("× Video Unavailable", self.screen_text(app))

    async def test_live_summary_wrap_preserves_a_manually_scrolled_queue(self):
        extra_rows = [self.entry(f"Extra{number:02}") for number in range(20)]
        async with self.populated_dashboard(extra_rows) as (app, pilot, emit, event):
            await pilot.resize_terminal(86, 24)
            emit(replace(event, position=9))
            queue = app.dashboard.query_one("#queue")
            summary = app.dashboard.query_one("#queue-summary")
            await self.wait_for(pilot, lambda: "Active 9/24" in self.screen_text(app) and summary.region.height == 1)
            selected = app.selected
            await pilot._post_mouse_events([MouseScrollDown], queue, offset=(12, 2), times=3)
            await self.wait_for(pilot, lambda: "Video Extra01" in self.screen_text(app))
            offset = queue.scroll_offset.y
            self.assertGreater(offset, 0)
            self.assertNotIn("▶ Goa Trance Collection", self.screen_text(app))

            emit(replace(event, position=10))
            await self.wait_for(pilot, lambda: summary.region.height == 2)
            await pilot.pause()
            self.assertEqual(queue.scroll_offset.y, offset)
            self.assertEqual(app.selected, selected)

    async def test_long_restored_queue_keeps_wide_footer_visible(self):
        self.disk.save([self.entry(str(number)) for number in range(30)])
        app = self.app()
        async with app.run_test(size=(150, 38)) as pilot:
            await self.ready(pilot, app)
            await pilot.pause()
            text = "\n".join(self.rendered_lines(app))
            self.assertIn("Start queue", text)
            self.assertIn("Help", text)
            self.assertIn("Queue restored · 30 Pending · 0 Interrupted", text)
            queue = app.dashboard.query_one("#queue")
            queue.focus()
            await pilot.press("ctrl+end")
            await pilot.pause()
            self.assertIn("Video 29", self.screen_text(app))
            self.assertTrue(await pilot.click("#help"))

    async def test_wrapped_destination_error_keeps_queue_and_footer_usable(self):
        async with self.populated_dashboard() as (app, pilot, _, _):
            existing_file = self.output / ("existing-recording-" * 5 + ".mp3")
            existing_file.touch()
            destination = app.dashboard.query_one("#destination")
            destination.value = str(existing_file)
            queue = app.dashboard.query_one("#queue")
            for width in (80, 100):
                with self.subTest(width=width):
                    await pilot.resize_terminal(width, 24)
                    await pilot.pause()
                    self.assertTrue(await pilot.click("#add-urls"))
                    self.assertIs(app.focused, destination)
                    rendered = self.screen_text(app)
                    for label in ("Correct the path or choose another directory", "Stop queue", "Help", "Quit"):
                        self.assertIn(label, rendered)
                    await pilot.press("tab", "tab", "end")
                    self.assertEqual(app.focused.id, "form-error-scroll")
                    self.assertIn(".mp3.", self.screen_text(app))
                    action_x = sum(column.width + 2 * queue.cell_padding for column in queue.ordered_columns[:-1]) + 1
                    self.assertTrue(await pilot.click(queue, offset=(action_x, 1)))
                    self.assertIsInstance(app.screen, RowMenu)
                    await pilot.press("escape")
                    self.assertIs(app.focused, queue)
                    await pilot.press("ctrl+end")
                    self.assertIn("× Video Una", self.screen_text(app))
                    await pilot.press("ctrl+home")
            self.assertTrue(await pilot.click("#primary"))
            await self.wait_for(pilot, lambda: not app.session.busy)

    async def test_compact_destination_error_is_readable_with_the_mouse(self):
        existing_file = self.output / "existing-file"
        existing_file.touch()
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            app.dashboard.query_one("#destination").value = str(existing_file)
            self.assertTrue(await pilot.click("#add-urls"))
            diagnostic = app.dashboard.query_one("#form-error-scroll")
            await pilot._post_mouse_events([MouseScrollDown], diagnostic, offset=(2, 0))
            await self.wait_for(pilot, lambda: f"{existing_file.name}." in self.screen_text(app))
            scrolled = self.screen_text(app)
            await pilot._post_mouse_events([MouseScrollUp], diagnostic, offset=(2, 0))
            await self.wait_for(pilot, lambda: "Correct the path" in self.screen_text(app))
            self.assertIn("Cannot use destination", scrolled + self.screen_text(app))

    async def test_wide_destination_error_keeps_layout_compact_and_clears_after_browse(self):
        async with self.populated_dashboard() as (app, pilot, _, _):
            await pilot.resize_terminal(220, 55)
            await pilot.pause()
            existing_file = self.output / ("existing-recording-" * 5 + ".mp3")
            existing_file.touch()
            destination = app.dashboard.query_one("#destination")
            destination.value = str(existing_file)
            self.assertTrue(await pilot.click("#add-urls"))
            await pilot.pause()
            self.assertIn("Cannot use destination", self.screen_text(app))
            transfer = app.dashboard.query_one("#transfer-panel")
            queue = app.dashboard.query_one("#queue-panel")
            gap_with_error = transfer.region.y - queue.region.bottom

            self.assertTrue(await pilot.click("#browse"))
            self.assertIsInstance(app.screen, Browse)
            self.assertTrue(await pilot.click("#choose"))
            await pilot.pause()
            self.assertEqual(destination.value, str(self.output))
            self.assertNotIn("Cannot use destination", self.screen_text(app))
            self.assertEqual(gap_with_error, 1)
            self.assertEqual(transfer.region.y, queue.region.bottom + 1)
            self.assertTrue(await pilot.click("#add-urls"))
            self.assertIsInstance(app.screen, AddURLs)

    async def test_wide_dashboard_after_resize(self):
        async with self.populated_dashboard() as (app, pilot, emit, event):
            queue = app.dashboard.query_one("#queue")
            self.assertIn("▶ Goa Trance Collection", "\n".join(self.rendered_lines(app)))
            await pilot.resize_terminal(100, 26)
            await self.wait_for(pilot, lambda: len(queue.columns) == 7)
            self.assertEqual(queue.region.height, queue.header_height + queue.row_count)
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
                last_row = next(number for number, line in enumerate(lines) if "× Video Unavailable" in line)
                self.assertIn("▶ Goa Trance Collection", active)
                self.assertIn("○ Video Yesterday", "\n".join(lines))
                self.assertIn("✓ Night City Mix", complete)
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
                self.assertLessEqual(transfer.region.y - last_row, 3)
                self.assertLessEqual(controls.region.y - transfer.region.bottom, 1)
            emit(replace(event, position=100, total=120))
            await pilot.resize_terminal(100, 24)
            await pilot.pause()
            row = next(line for line in self.rendered_lines(app) if "Downloading" in line and "61%" in line)
            self.assertIn("Downloading 100/120", row)

    async def test_wide_settings_and_job_details_after_resize(self):
        async with self.populated_dashboard() as (app, pilot, _, _):
            queue = app.dashboard.query_one("#queue")
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
            await pilot.pause()
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
            await self.wait_for(pilot, task.done)
            self.assertEqual(task.result(), "single")
