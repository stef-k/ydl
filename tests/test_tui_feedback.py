"""Exercise queue controls and diagnostics while the dashboard is on screen."""

import asyncio
import hashlib
from dataclasses import replace
from threading import Event as ThreadEvent
from unittest.mock import patch

from textual.widgets import Button, TabbedContent, TextArea

from ydl import config, jobs, runner
from ydl.models import Event, Result, Settings, YdlError
from ydl.tui import YdlApp
from ydl.tui_dialogs import Confirm, SettingsPanel
from ydl.tui_jobs import Details, RowMenu
from ydl.tui_queue import QueueFile, missing_plan_message

from tui_pilot import PilotCase, URL


class FeedbackTests(PilotCase):
    async def test_add_shortcut_submits_only_outside_text_fields(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            editor = app.dashboard.query_one("#draft", TextArea)
            queue = app.dashboard.query_one("#queue")
            queue.focus()
            await pilot.press("a")
            self.assertIs(app.focused, editor)
            self.assertIn("Enter at least one YouTube URL", self.screen_text(app))
            await pilot.press("a")
            self.assertEqual(editor.text, "a")
            self.assertNotIn("Enter at least one YouTube URL", self.screen_text(app))

            destination = app.dashboard.query_one("#destination")
            destination.value = str(self.output)
            destination.focus()
            await pilot.press("end", "a")
            self.assertEqual(destination.value, f"{self.output}a")
            self.assertEqual(app.session.index.rows, [])
            editor.load_text(URL + "A")
            queue.focus()
            await pilot.press("a")
            await self.wait_for(pilot, lambda: "Added 1 URL" in self.screen_text(app))
            self.assertIs(app.screen, app.dashboard)
            self.assertEqual(editor.text, "")
            self.assertEqual([row.url for row in self.disk.read()], [URL + "A"])

    async def test_quit_warns_if_add_enters_recovery_after_confirmation(self):
        draft = f"{URL}Added\ninvalid"
        app = self.app(draft)
        syncing, release_sync = ThreadEvent(), ThreadEvent()

        def reject_sync(_path):
            syncing.set()
            if not release_sync.wait(5):
                raise RuntimeError("Test did not release the Add directory sync")
            raise OSError("Add directory sync failed after replacement")

        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            with patch.object(config, "sync_directories", reject_sync):
                self.assertTrue(await pilot.click("#add-urls"))
                try:
                    await self.wait_for(pilot, syncing.is_set)
                    await pilot.press("ctrl+q")
                    self.assertEqual(str(app.screen.query_one("#confirm", Button).label), "Stop and quit")
                    self.assertTrue(await pilot.click("#confirm"))
                    self.assertTrue(app.session.closing)
                finally:
                    release_sync.set()
                await self.wait_for(pilot, lambda: app.return_value is not None or
                                    (isinstance(app.screen, Confirm) and app.screen.ready))

            self.assertIsNone(app.return_value, "Quit must warn before discarding the recovery draft")
            self.assertIn("Unresolved draft text", self.screen_text(app))
            self.assertEqual(str(app.focused.label), "Stay")
            await pilot.press("enter")
            self.assertIs(app.screen, app.dashboard)
            editor = app.dashboard.query_one(TextArea)
            self.assertEqual(editor.text, draft)
            self.assertTrue(await pilot.click("#recover"))
            await self.wait_for(pilot, lambda: not editor.read_only)
            self.assertEqual(editor.text, "invalid")
            self.assertEqual([row.url for row in self.disk.read()], [URL + "Added"])
            self.assertFalse(app.session.busy)

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
                    self.assertTrue(await pilot.click("#add-urls"))
                    await self.wait_for(pilot, lambda: isinstance(app.screen, Confirm) and app.screen.ready)
                    await pilot.click("#cancel")
                    await self.wait_for(pilot, lambda: "Duplicate was not added" in self.screen_text(app))
                    self.assertIs(app.screen, app.dashboard)
                    self.assertEqual(app.screen.query_one("#draft", TextArea).text, "https://youtu.be/A?si=tracking")
                    self.assertEqual(self.disk.read(), [row])
                finally:
                    resume.set()
                    await self.ready(pilot, app)

    async def test_add_duplicate_waits_for_quit_answer(self):
        for stay in (True, False):
            with self.subTest(stay=stay):
                await self.check_duplicate_during_quit(stay=stay)

    async def check_duplicate_during_quit(self, *, stay):
        first, pending = self.entry("First"), self.entry("Pending")
        self.disk.save([first, pending])
        app = self.app(pending.url)
        saving, release_save = ThreadEvent(), ThreadEvent()
        save = self.disk.save

        def hold_active_save(rows):
            if rows[0].status == "active":
                saving.set()
                if not release_save.wait(5):
                    raise RuntimeError("Test did not release the Active save")
            save(rows)

        async def download(*_args, **_kwargs):
            await asyncio.Future()

        with (patch.object(self.disk, "save", hold_active_save), patch.object(runner, "download", download)):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(pilot, app)
                try:
                    self.assertTrue(await pilot.click("#primary"))
                    await self.wait_for(pilot, saving.is_set)
                    self.assertTrue(await pilot.click("#add-urls"))
                    self.assertTrue(await pilot.click("#quit"))
                    quit_dialog = app.screen
                    self.assertEqual(quit_dialog.heading, "Quit ydl")
                finally:
                    release_save.set()
                try:
                    await self.wait_for(pilot, lambda: app.session.active_id == first.id)
                    self.assertIs(app.screen, quit_dialog)
                    if stay:
                        await pilot.press("enter")
                        await self.wait_for(pilot, lambda: isinstance(app.screen, Confirm) and app.screen.ready)
                        self.assertEqual(app.screen.heading, "Duplicate URL")
                        await pilot.press("enter")
                        await self.wait_for(pilot, lambda: "Duplicate was not added" in self.screen_text(app))
                        self.assertEqual(app.dashboard.query_one(TextArea).text, pending.url)
                    else:
                        self.assertTrue(await pilot.click("#confirm"))
                        await self.wait_for(pilot, lambda: app.return_value is not None)
                        self.assertEqual(app.return_value, 0)
                finally:
                    await app.session.stop()
                    if app.adding:
                        while isinstance(app.screen, Confirm):
                            await pilot.press("escape")
                        await self.wait_for(pilot, lambda: not app.adding)
        self.assertEqual([row.url for row in self.disk.read()], [first.url, pending.url])

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
                self.assertTrue(await pilot.click("#add-urls"))
                await self.wait_for(pilot, lambda: isinstance(app.screen, Confirm) and app.screen.ready)
                confirmation = app.screen
                self.assertIn(second.url, confirmation.question)
                await pilot.click("#modal-stop")
                await self.wait_for(pilot, lambda: not app.session.busy)
                await pilot.click("#confirm")
                await self.wait_for(pilot, lambda: isinstance(app.screen, Confirm)
                                    and app.screen is not confirmation and app.screen.ready)
                self.assertIn(first.url, app.screen.question)
                await pilot.click("#cancel")
                await self.wait_for(pilot, lambda: "Duplicate was not added" in self.screen_text(app))
                self.assertIs(app.screen, app.dashboard)
                self.assertEqual(app.screen.query_one("#draft", TextArea).text, first.url)
                self.assertEqual([row.url for row in self.disk.read()], [first.url, second.url, second.url])

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
            self.assertTrue(await pilot.click("#add-urls"))
            await self.wait_for(pilot, lambda: isinstance(app.screen, Confirm) and app.screen.ready)
            self.paths.config.write_text("quality = [")
            finish_check.set()
            await self.wait_for(pilot, lambda: bool(app.session.backend.config_error))
            await pilot.click("#confirm")
            await self.wait_for(pilot, lambda: app.screen is app.dashboard and not app.screen.query_one("#draft").read_only)
            self.assertEqual(self.disk.read(), [row])
            editor = app.screen.query_one("#draft", TextArea)
            self.assertEqual(editor.text, draft)
            self.assertIs(app.focused, editor)
            self.assertTrue(app.screen.query_one("#add-urls").disabled)
            await pilot.pause()
            error = app.screen.query_one("#form-error").region
            self.assertIn(f"Configuration error at {self.paths.config}", self.rendered_lines(app)[error.y])

    async def test_invalid_destination_shows_inline_error_and_keeps_draft(self):
        row = self.entry("Queued")
        self.disk.save([row])
        draft = self.entry("A").url
        app = self.app(prefill=draft)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            await self.wait_for(pilot, lambda: "Queue restored" in self.screen_text(app))
            destination = app.screen.query_one("#destination")
            destination.value = ""
            await pilot.click("#add-urls")
            await pilot.pause()
            self.assertIs(app.screen, app.dashboard)
            self.assertIs(app.focused, destination)
            self.assertEqual(app.screen.query_one("#draft", TextArea).text, draft)
            self.assertIn("Output must name a directory", self.screen_text(app))
            self.assertNotIn("Queue restored", self.screen_text(app))
            await pilot.press("x")
            self.assertNotIn("Output must name a directory", self.screen_text(app))
            self.assertNotIn("Queue restored", self.screen_text(app))

    async def test_restore_completion_does_not_override_destination_error(self):
        self.disk.save([self.entry("Queued")])
        app = self.app(prefill=self.entry("A").url)
        resume = asyncio.Event()
        inspect = jobs.inspect_job

        async def verify(*args, **kwargs):
            await resume.wait()
            return await inspect(*args, **kwargs)

        with patch.object(jobs, "inspect_job", side_effect=verify):
            async with app.run_test(size=(80, 24)) as pilot:
                try:
                    await self.wait_for(pilot, lambda: "Verifying" in self.screen_text(app))
                    app.screen.query_one("#destination").value = ""
                    self.assertTrue(await pilot.click("#add-urls"))
                    self.assertIn("Output must name a directory", self.screen_text(app))
                finally:
                    resume.set()
                await self.ready(pilot, app)
                await pilot.pause()
                self.assertIn("Output must name a directory", self.screen_text(app))
                self.assertNotIn("Queue restored", self.screen_text(app))
                await pilot.press("x")
                self.assertNotIn("Output must name a directory", self.screen_text(app))
                self.assertNotIn("Queue restored", self.screen_text(app))

    async def test_restore_completion_does_not_reinstate_notice_after_removal(self):
        removed, remaining = self.entry("Removed"), self.entry("Remaining")
        self.disk.save([removed, remaining])
        app = self.app()
        resume = asyncio.Event()
        inspect = jobs.inspect_job

        async def verify(*args, **kwargs):
            await resume.wait()
            return await inspect(*args, **kwargs)

        with patch.object(jobs, "inspect_job", side_effect=verify):
            async with app.run_test(size=(80, 24)) as pilot:
                try:
                    await self.wait_for(pilot, lambda: "Verifying" in self.screen_text(app))
                    app.dashboard.query_one("#queue").focus()
                    await pilot.press("delete")
                    await self.wait_for(pilot, lambda: isinstance(app.screen, Confirm) and app.screen.ready)
                    self.assertTrue(await pilot.click("#confirm"))
                    await self.wait_for(pilot, lambda: app.session.index.rows == [remaining])
                    self.assertEqual(self.disk.read(), [remaining])
                finally:
                    resume.set()
                await self.ready(pilot, app)
                await pilot.pause()
                self.assertNotIn("Queue restored", self.screen_text(app))

    async def test_repairing_startup_config_does_not_reveal_restore_notice(self):
        self.paths.config.write_text("quality = [")
        self.disk.save([self.entry("Queued")])
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ready(pilot, app)
            await pilot.pause()
            self.assertIn("Configuration error", self.screen_text(app))
            self.assertTrue(await pilot.click("#settings"))
            await self.wait_for(pilot, lambda: isinstance(app.screen, SettingsPanel) and app.screen.ready)
            app.screen.query_one("#settings-output").value = str(self.output)
            self.assertTrue(await pilot.click("#save-settings"))
            await self.wait_for(pilot, lambda: app.screen is app.dashboard)
            await pilot.pause()
            text = self.screen_text(app)
            self.assertIn("Check pending", text)
            self.assertNotIn("Configuration error", text)
            self.assertNotIn("Queue restored", text)

    async def test_details_shows_current_verified_count_after_recheck(self):
        row = self.entry("A")
        self.disk.save([row])
        store = jobs.JobStore(self.paths.state)
        item = jobs.make_item(1, "A", "Track", False)
        mp3 = self.output / f"{item.stem}.mp3"
        content = b"saved MP3"
        item.outputs["mp3"] = {"name": mp3.name, "sha256": hashlib.sha256(content).hexdigest()}

        async def download(*args, emit, **kwargs):
            mp3.write_bytes(content)
            with store.claim(row.id, new=True):
                job = jobs.Job.create(row.id, Settings("single", "0", False, self.output), [item],
                                      enqueued_base=row.output)
                store.save(job)
            emit(Event("settings", title=item.title, settings=job.settings, total=1))
            return Result(self.output, saved=1, job_id=row.id)

        app = self.app()
        with patch.object(runner, "download", side_effect=download):
            async with app.run_test(size=(100, 30)) as pilot:
                await self.ready(pilot, app)
                self.assertTrue(await pilot.click("#primary"))
                await self.wait_for(pilot, lambda: app.session.index.view(row.id).status == "Complete"
                                    and not app.session.busy)
                app.dashboard.query_one("#queue").focus()
                await pilot.press("enter")
                await self.wait_for(pilot, lambda: isinstance(app.screen, Details) and app.screen.ready)
                self.assertRegex(self.screen_text(app), r"Saved +1")
                await pilot.press("escape")

                mp3.unlink()
                await pilot.press("f2")
                await self.wait_for(pilot, lambda: isinstance(app.screen, RowMenu) and app.screen.ready)
                self.assertTrue(await pilot.click("#recheck"))
                await self.wait_for(pilot, lambda: app.session.index.view(row.id).status == "Interrupted")
                await self.ready(pilot, app)
                await pilot.press("enter")
                await self.wait_for(pilot, lambda: isinstance(app.screen, Details) and app.screen.ready)
                text = self.screen_text(app)
                self.assertRegex(text, r"Status +Interrupted")
                self.assertRegex(text, r"Verified +0/1")
                self.assertNotRegex(text, r"Saved +1")

                await pilot.press("escape")
                store.path(row.id).unlink()
                await pilot.press("f2")
                await self.wait_for(pilot, lambda: isinstance(app.screen, RowMenu) and app.screen.ready)
                self.assertTrue(await pilot.click("#recheck"))
                await self.ready(pilot, app)
                await pilot.press("enter")
                await self.wait_for(pilot, lambda: isinstance(app.screen, Details) and app.screen.ready)
                self.assertTrue(await pilot.click("#--content-tab-info-pane"))
                text = self.screen_text(app)
                self.assertIn("Missing saved plan", text)
                self.assertRegex(text, r"Core job ID +—")
                self.assertRegex(text, r"Resolved scope +—")
                self.assertRegex(text, r"Resolved output +—")

    async def test_recheck_shows_resolved_collision_only_in_history(self):
        diagnostic = "Refusing to overwrite an existing output"
        row = replace(self.entry("A"), status="failed", diagnostic=diagnostic)
        self.disk.save([row])
        store = jobs.JobStore(self.paths.state)
        item = jobs.make_item(1, "A", "Track", False)
        mp3 = self.output / f"{item.stem}.mp3"
        content = b"saved MP3"
        mp3.write_bytes(b"occupied")
        item.outputs["mp3"] = {"name": mp3.name, "sha256": hashlib.sha256(content).hexdigest()}
        with store.claim(row.id, new=True):
            store.save(jobs.Job.create(row.id, Settings("single", "0", False, self.output), [item],
                                      enqueued_base=row.output))

        app = self.app()
        async with app.run_test(size=(150, 35)) as pilot:
            await self.ready(pilot, app)
            self.assertEqual(app.session.index.view(row.id).status, "Needs attention")
            mp3.write_bytes(content)
            app.dashboard.query_one("#queue").focus()
            await pilot.press("f2")
            await self.wait_for(pilot, lambda: isinstance(app.screen, RowMenu) and app.screen.ready)
            self.assertTrue(await pilot.click("#recheck"))
            await self.ready(pilot, app)
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: isinstance(app.screen, Details) and app.screen.ready)
            text = self.screen_text(app)
            self.assertRegex(text, r"Status +Complete")
            self.assertRegex(text, r"Verified +1/1")
            self.assertNotIn(diagnostic, text)
            self.assertTrue(await pilot.click("#--content-tab-info-pane"))
            self.assertIn(f"Last diagnostic  {diagnostic}", self.screen_text(app))

    async def test_recheck_clears_saved_missing_plan_diagnostic(self):
        row = self.entry("A")
        store = jobs.JobStore(self.paths.state)
        diagnostic = missing_plan_message(store, row.id)
        self.disk.save([replace(row, status="failed", diagnostic=diagnostic)])
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ready(pilot, app)
            self.assertEqual(app.session.index.view(row.id).status, "Needs attention")
            item = jobs.make_item(1, "A", "Track", False)
            with store.claim(row.id, new=True):
                store.save(jobs.Job.create(row.id, Settings("single", "0", False, self.output),
                                          [item], enqueued_base=row.output))
            app.dashboard.query_one("#queue").focus()
            await pilot.press("f2")
            await self.wait_for(pilot, lambda: isinstance(app.screen, RowMenu) and app.screen.ready)
            self.assertTrue(await pilot.click("#recheck"))
            await self.ready(pilot, app)
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: isinstance(app.screen, Details) and app.screen.ready)
            self.assertFalse(app.screen.query_one("#details-retry").disabled)
            self.assertNotIn("Missing saved plan", self.screen_text(app))
            self.assertTrue(await pilot.click("#--content-tab-info-pane"))
            self.assertIn("Last diagnostic  —", self.screen_text(app))
            self.assertEqual(self.disk.read()[0].diagnostic, "")

    async def test_settings_focuses_invalid_output_from_backend_tab(self):
        app = self.app()
        file = self.root / "existing\nrecording\u2028test.mp3"
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
                if value:
                    self.assertIn("existing recording test.mp3", str(error.render()))
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
