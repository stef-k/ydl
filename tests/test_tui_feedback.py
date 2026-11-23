"""Exercise queue controls and diagnostics while the dashboard is on screen."""

import asyncio
import hashlib
from dataclasses import replace
from threading import Event as ThreadEvent
from unittest.mock import patch

from textual.widgets import TabbedContent, TextArea

from ydl import config, jobs, runner
from ydl.models import Result, Settings, YdlError
from ydl.tui import YdlApp
from ydl.tui_dialogs import AddURLs, Confirm, SettingsPanel
from ydl.tui_queue import QueueFile

from tui_pilot import PilotCase


class FeedbackTests(PilotCase):
    async def test_quit_cancels_receipt_verification_and_waits_for_cleanup(self):
        row = replace(self.entry("A"), status="interrupted")
        self.disk.save([row])
        item = jobs.make_item(1, "A", "Song", False)
        mp3 = self.output / f"{item.stem}.mp3"
        mp3.touch()
        item.outputs["mp3"] = {"name": mp3.name, "sha256": hashlib.sha256(b"").hexdigest()}
        store = jobs.JobStore(self.paths.state)
        with store.claim(row.id, new=True):
            store.save(jobs.Job.create(row.id, Settings("single", "0", False, self.output), [item],
                                      enqueued_base=row.output))
        verifying, cancelling, release = ThreadEvent(), ThreadEvent(), ThreadEvent()
        sha256 = jobs._sha256

        def slow_hash(path, *, stop):
            verifying.set()
            while not release.wait(0.01):
                if stop.is_set():
                    cancelling.set()
            return sha256(path, stop=stop)

        app = self.app()
        with patch.object(jobs, "_sha256", side_effect=slow_hash):
            async with app.run_test(size=(80, 24)) as pilot:
                try:
                    await self.wait_for(pilot, lambda: verifying.is_set() and app.session.backend.check is None)
                    await pilot.press("ctrl+q")
                    await self.wait_for(pilot, cancelling.is_set)
                    self.assertIsNone(app.return_code)
                finally:
                    release.set()
                await self.wait_for(pilot, lambda: app.return_code == 0)
        self.assertEqual(self.disk.read(), [row])

    async def test_stop_at_worker_entry_preserves_failed_hint(self):
        row = replace(self.entry("A"), status="failed_before_plan")
        self.disk.save([row])
        app = self.app()
        scheduled = False
        schedule = asyncio.get_running_loop().call_soon
        refresh = app.session.changed

        def changed(**kwargs):
            nonlocal scheduled
            refresh(**kwargs)
            if app.session.index.view(row.id).status == "Active" and not scheduled:
                scheduled = True
                schedule(app.action_stop)

        app.session.changed = changed
        with patch.object(runner, "download", return_value=Result(saved=1)) as download:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(pilot, app)
                await pilot.click("#retry-failed")
                await self.wait_for(pilot, lambda: scheduled and not app.session.busy)
                download.assert_not_called()
                self.assertEqual(self.disk.read(), [row])

    async def test_duplicate_of_pending_row_prompts_during_restore(self):
        row = self.entry("A")
        self.disk.save([row])
        app = self.app(prefill="https://youtu.be/A?si=tracking")
        resume = asyncio.Event()
        started = asyncio.Event()
        inspect = jobs.inspect_job

        async def verify(*args, **kwargs):
            started.set()
            await resume.wait()
            return await inspect(*args, **kwargs)

        with patch.object(jobs, "inspect_job", side_effect=verify):
            async with app.run_test(size=(80, 24)) as pilot:
                try:
                    await self.wait_for(pilot, started.is_set)
                    await self.submit(pilot)
                    self.assertIsInstance(app.screen, Confirm)
                    await pilot.click("#cancel")
                    await self.wait_for(pilot, lambda: not app.screen.saving)
                    self.assertEqual(self.disk.read(), [row])
                finally:
                    resume.set()
                    await self.ready(pilot, app)

    async def test_add_rechecks_pending_duplicates_after_confirmation(self):
        first, second = self.entry("A"), self.entry("B")
        self.disk.save([first, second])
        draft = f"{first.url}\n{second.url}"
        app = self.app(prefill=draft)

        async def download(*_args, **_kwargs):
            await asyncio.Future()

        with patch.object(runner, "download", side_effect=download):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(pilot, app)
                await pilot.click("#primary")
                await self.wait_for(pilot, lambda: app.session.active_id == first.id)
                await self.submit(pilot)
                await self.wait_for(pilot, lambda: isinstance(app.screen, Confirm) and app.screen.ready)
                confirmation = app.screen
                self.assertIn(second.url, confirmation.question)
                await pilot.click("#modal-stop")
                await self.wait_for(pilot, lambda: not app.session.busy)
                await pilot.click("#cancel")
                await self.wait_for(pilot, lambda: app.screen is not confirmation and app.screen.ready and
                                    (not isinstance(app.screen, AddURLs) or not app.screen.saving))
                self.assertIsInstance(app.screen, Confirm)
                self.assertIn(first.url, app.screen.question)
                await pilot.click("#cancel")
                await self.wait_for(pilot, lambda: isinstance(app.screen, AddURLs) and not app.screen.saving)
                self.assertEqual(app.screen.query_one(TextArea).text, draft)
                self.assertEqual([row.url for row in self.disk.read()], [first.url, second.url])

    async def test_add_confirmation_preserves_draft_when_config_becomes_invalid(self):
        row = self.entry("A")
        self.disk.save([row])
        draft = f"{row.url}\n{self.entry('B').url}"
        app = self.app(prefill=draft)
        finish_check = asyncio.Event()

        async def check(*_args, **_kwargs):
            await finish_check.wait()

        self.backends.side_effect = check
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            await self.submit(pilot)
            await self.wait_for(pilot, lambda: isinstance(app.screen, Confirm) and app.screen.ready)
            confirmation = app.screen
            self.paths.config.write_text("quality = [")
            finish_check.set()
            await self.wait_for(pilot, lambda: bool(app.session.backend.config_error))
            await pilot.click("#confirm")
            await self.wait_for(pilot, lambda: app.screen is not confirmation and
                                (not isinstance(app.screen, AddURLs) or not app.screen.saving))
            self.assertEqual(self.disk.read(), [row])
            self.assertIsInstance(app.screen, AddURLs)
            self.assertEqual(app.screen.query_one(TextArea).text, draft)
            self.assertIs(app.focused, app.screen.query_one(TextArea))
            self.assertTrue(app.screen.query_one("#submit-add").disabled)
            await pilot.pause()
            error = app.screen.query_one("#add-error").region
            dialog_lines = self.rendered_lines(app)[error.y:error.bottom]
            self.assertIn(f"Configuration error at {self.paths.config}", "\n".join(dialog_lines))

    async def test_invalid_destination_shows_inline_error_and_keeps_draft(self):
        draft = self.entry("A").url
        app = self.app(prefill=draft)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            destination = app.screen.query_one("#destination")
            destination.value = ""
            await pilot.click("#add-urls")
            await pilot.pause()
            self.assertIs(app.screen, app.dashboard)
            self.assertIs(app.focused, destination)
            self.assertEqual(app.screen.query_one("#draft", TextArea).text, draft)
            self.assertIn("Output must name a directory", self.screen_text(app))
            await pilot.press("x")
            self.assertNotIn("Output must name a directory", self.screen_text(app))

    async def test_settings_focuses_invalid_output_from_backend_tab(self):
        app = self.app()
        file = self.root / "existing-file"
        file.touch()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            await pilot.click("#settings")
            await self.wait_for(pilot, lambda: isinstance(app.screen, SettingsPanel) and app.screen.ready)
            settings = app.screen
            output = settings.query_one("#settings-output")
            tabs = settings.query_one(TabbedContent)
            error = settings.query_one("#settings-error")
            cases = [("", "Output must name a directory"), (str(file), "Cannot use destination")]
            for value, message in cases:
                output.value = value
                await pilot.click("#--content-tab-backend-settings")
                self.assertEqual(tabs.active, "backend-settings")
                await pilot.click("#save-settings")
                await self.wait_for(pilot, lambda: app.screen is not settings or message in str(error.render()))
                await pilot.pause()
                self.assertIs(app.screen, settings, value)
                self.assertIn(message, self.screen_text(app))
                self.assertEqual(tabs.active, "general")
                self.assertIs(app.focused, output)
                self.assertEqual(output.value, value)
                self.assertFalse(self.paths.config.exists())
            new_directory = self.root / "new" / "downloads"
            output.value = str(new_directory)
            await pilot.click("#save-settings")
            await self.wait_for(pilot, lambda: app.screen is app.dashboard)
            self.assertEqual(config.load(self.paths.config).output, str(new_directory))
            self.assertFalse(new_directory.exists())

    async def test_backend_failure_replaces_restored_notice(self):
        self.disk.save([self.entry("A")])
        finish = asyncio.Event()

        async def check(*args, **kwargs):
            await finish.wait()
            raise YdlError("Release service returned HTTP 503")

        self.backends.side_effect = check
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            await pilot.pause()
            scroll = app.screen.query_one("#notice-scroll")
            self.assertIn("ℹ Queue restored · 1 Pending · 0 Interrupted", self.screen_text(app))
            self.assertEqual(scroll.styles.border_left[0], "solid")
            await pilot.resize_terminal(150, 38)
            await pilot.pause()
            self.assertEqual(scroll.styles.border_top[0], "solid")
            finish.set()
            await self.wait_for(pilot, lambda: app.session.backend.check is None)
            await pilot.pause()
            text = self.screen_text(app)
            self.assertIn("Release service returned HTTP 503", text)
            self.assertNotIn("Queue restored", text)
            self.assertFalse(scroll.has_class("-restored"))

    async def test_recovery_notice_scrolls_without_hiding_controls(self):
        self.paths = replace(self.paths, state=self.root / ("a" * 100) / ("b" * 100))
        self.disk = QueueFile(self.paths.state)
        self.disk.path.parent.mkdir(parents=True)
        self.disk.path.write_text('{"schema_version": 7, "entries": []}')
        app = YdlApp(self.paths, disk=self.disk)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.wait_for(pilot, lambda: bool(app.session.index.error))
            await pilot.pause()
            self.assertIn("unsupported queue schema version", self.screen_text(app))
            scroll = app.screen.query_one("#notice-scroll")
            scroll.focus()
            await pilot.press("end")
            await pilot.pause()
            text = self.screen_text(app)
            self.assertIn("move the file aside and reopen", text)
            self.assertIn("Recover queue", text)
            self.assertIn("Quit", text)
            await pilot.press("up")
            await pilot.pause()
            self.assertIn("tui-queue.json", self.screen_text(app))
