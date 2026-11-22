"""Exercise terminal interactions through the installed Textual frontend."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Button, Input, RadioSet, Select, TabbedContent, TextArea

from ydl import backend, config, jobs, runner
from ydl.models import Decision, Event, Result, Settings, YdlError
from ydl.tui import YdlApp
from ydl.tui_dialogs import AddURLs, Browse, Confirm, HelpPanel, RunnerPrompt, SettingsPanel
from ydl.tui_jobs import Details, RecentJobs, RowMenu
from ydl.tui_queue import Entry, QueueFile

URL = "https://www.youtube.com/watch?v="


class PilotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.paths = config.Paths(self.root / "config.toml", self.root / "data", self.root / "state")
        self.disk = QueueFile(self.paths.state)
        self.output = self.root / "output"
        self.output.mkdir()
        self.backends = self.enterContext(patch.object(backend, "ensure_backend", return_value=None))
        self.versions = self.enterContext(patch.object(backend, "backend_version", return_value="2026.01.01"))

    def entry(self, name, **overrides):
        values = dict(scope="auto", quality="0", keep_video=False, output=self.output)
        return Entry.from_url(URL + name, **{**values, **overrides})

    def app(self, prefill=None):
        return YdlApp(self.paths, prefill, self.disk)

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

    async def test_prefill_batch_validation_and_restore(self):
        draft = "https://youtu.be/A?t=2\n\nnot-a-url\nhttps://www.youtube.com/watch?v=A\nhttps://youtu.be/B"
        app = self.app(draft)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            self.assertEqual(app.focused.id, "draft")
            self.assertEqual(app.focused.text, draft)
            self.assertEqual(app.session.index.rows, [])
            await self.submit(pilot)
            await self.wait_for(pilot, lambda: isinstance(app.screen, Confirm))
            self.assertEqual(app.focused.id, "cancel")
            await pilot.press("enter")
            await self.wait_for(pilot, lambda: len(app.session.index.rows) == 2)
            await self.wait_for(pilot, lambda: not app.screen.saving)
            self.assertEqual(app.screen.query_one(TextArea).text.splitlines(), ["not-a-url", URL + "A"])
            await pilot.press("escape")
            self.assertEqual(app.screen.query_one("#draft", TextArea).text.splitlines(), ["not-a-url", URL + "A"])
        saved = self.disk.read()
        self.assertEqual([row.url for row in saved], [URL + "A", URL + "B"])
        reopened = self.app()
        async with reopened.run_test() as pilot:
            await self.ready(pilot, reopened)
            self.assertEqual(reopened.selected, saved[0].id)
            self.assertEqual(reopened.session.index.rows, saved)
            self.assertFalse(reopened.session.busy)
            self.assertIn("2 Pending", reopened.session.index.notice)

    async def test_serial_attempt_runs_selected_options_and_latest_order(self):
        first, second, third = [self.entry(name) for name in "ABC"]
        self.disk.save([first, second, third])
        calls = []
        released = asyncio.Event()
        app = self.app()

        async def download(request, *_args, job_id, backend_ready, **_kwargs):
            self.assertTrue(backend_ready)
            durable = self.disk.read()
            self.assertEqual(next(row.status for row in durable if row.id == job_id), "active")
            calls.append((job_id, request))
            if job_id == first.id:
                await released.wait()
            return Result(saved=1)

        with patch.object(runner, "download", side_effect=download):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(pilot, app)
                self.assertTrue(await pilot.click("#primary"))
                await self.wait_for(pilot, lambda: len(calls) == 1)
                self.assertFalse(app.can_row("remove", first.id))
                await app.move(third.id, -1)
                released.set()
                await self.wait_for(pilot, lambda: not app.session.busy)
                self.assertEqual([job_id for job_id, _ in calls], [first.id, third.id, second.id])
                request = calls[0][1]
                self.assertEqual((request.scope, request.quality, request.keep_video, request.output),
                                 (None, "0", False, str(self.output)))
                self.assertEqual([entry.status for entry in self.disk.read()], ["complete"] * 3)
                self.backends.assert_awaited_once()

    async def test_scope_default_dismiss_and_stop_with_mouse(self):
        a, b = self.entry("A&list=Playlist01"), self.entry("B")
        self.disk.save([a, b])
        app = self.app()
        attempts = []

        async def download(_request, *_args, job_id, decide, **_kwargs):
            attempts.append(job_id)
            if job_id == a.id:
                decision = Decision("Download", (("single", "This video"), ("playlist", "Entire playlist")),
                                    "single", "scope", "A", "Playlist01")
                try:
                    await decide(decision)
                except runner.DecisionDismissed:
                    raise runner.AttemptDismissed(planned=False) from None
                except asyncio.CancelledError:
                    raise runner.AttemptStopped(planned=False) from None
            return Result(saved=1)

        with patch.object(runner, "download", side_effect=download):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(pilot, app)
                await pilot.click("#primary")
                await self.wait_for(pilot, lambda: isinstance(app.screen, RunnerPrompt) and app.screen.ready)
                self.assertEqual(app.screen.query_one(RadioSet).pressed_index, 0)
                self.assertTrue(await pilot.click("#modal-stop"))
                await self.wait_for(pilot, lambda: not app.session.busy)
                self.assertEqual(attempts, [a.id])
                self.assertEqual([row.status for row in self.disk.read()], ["pending", "pending"])
                await pilot.click("#primary")
                await self.wait_for(pilot, lambda: isinstance(app.screen, RunnerPrompt) and app.screen.ready)
                await pilot.press("escape")
                await self.wait_for(pilot, lambda: not app.session.busy)
                self.assertEqual(attempts, [a.id, a.id, b.id])
                self.assertEqual([row.status for row in self.disk.read()], ["cancelled", "complete"])

    async def test_menu_columns_details_and_focus(self):
        entries = [self.entry(f"LongTitle{i}") for i in range(12)]
        self.disk.save(entries)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            await pilot.press("d", "s", "a", "r", "c", "enter")
            self.assertEqual(app.focused.text, "dsarc\n")
            self.assertIs(app.screen, app.dashboard)
            queue = app.dashboard.query_one("#queue")
            self.assertEqual(len(queue.columns), 4)
            action_column = queue.ordered_columns[-1]
            action_x = sum(column.width + 2 * queue.cell_padding for column in queue.ordered_columns[:-1]) + 1
            self.assertEqual(action_column.key.value, "actions")
            self.assertTrue(await pilot.click("#queue", offset=(action_x, 1)))
            self.assertIsInstance(app.screen, RowMenu)
            self.assertTrue(app.screen.query_one("#up", Button).disabled)
            self.assertTrue(await pilot.click("#details"))
            await self.wait_for(pilot, lambda: isinstance(app.screen, Details) and app.screen.ready)
            self.assertEqual(app.screen.row_id, entries[0].id)
            await pilot.press("escape")
            self.assertIs(app.focused, queue)
            await pilot.press("f2")
            self.assertIsInstance(app.screen, RowMenu)
            await pilot.click("#down")
            await self.wait_for(pilot, lambda: self.disk.read()[1].id == entries[0].id)
            self.assertEqual(app.selected, entries[0].id)
            await pilot.resize_terminal(112, 26)
            await self.wait_for(pilot, lambda: len(queue.columns) == 7)
            await pilot.click("#help")
            self.assertIsInstance(app.screen, HelpPanel)
            await pilot.press("escape")
            self.assertEqual(app.focused.id, "help")

    async def test_settings_browse_and_repair_keep_draft_and_scope(self):
        self.paths.config.write_text("[[broken")
        row = self.entry("A")
        self.disk.save([row])
        app = self.app(URL + "B")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            self.assertEqual(app.session.index.rows, [row])
            self.assertIn(str(self.paths.config), app.session.backend.config_error)
            self.assertTrue(app.dashboard.query_one("#add-urls", Button).disabled)
            self.assertTrue(app.dashboard.query_one("#primary", Button).disabled)
            self.backends.assert_not_called()
            app.dashboard.query_one("#scope", Select).value = "playlist"
            await pilot.click("#settings")
            await self.wait_for(pilot, lambda: isinstance(app.screen, SettingsPanel) and app.screen.ready)
            field = app.screen.query_one("#settings-output", Input)
            field.value = str(self.output / "not-created")
            await pilot.click("#settings-browse")
            self.assertIsInstance(app.screen, Browse)
            await pilot.press("escape")
            self.assertEqual(field.value, str(self.output / "not-created"))
            await pilot.click("#settings-browse")
            await pilot.click("#choose")
            self.assertEqual(field.value, str(self.output))
            app.screen.query_one(TabbedContent).active = "backend-settings"
            app.screen.query_one("#settings-channel", Select).value = "stable"
            app.screen.query_one("#settings-update").value = False
            await pilot.click("#save-settings")
            await self.wait_for(pilot, lambda: app.screen is app.dashboard)
            prefs = config.load(self.paths.config)
            self.assertEqual((prefs.output, prefs.auto_update, prefs.channel), (str(self.output), False, "stable"))
            self.assertEqual(app.dashboard.query_one("#scope", Select).value, "playlist")
            self.assertEqual(app.dashboard.query_one("#draft", TextArea).text, URL + "B")
            self.assertEqual(self.disk.read(), [row])
            self.assertEqual(app.session.backend.status.state, "Check pending")
            self.assertFalse(app.session.busy)
            self.backends.assert_not_called()

    async def test_add_save_uncertainty_keeps_draft_and_recovery_stays_idle(self):
        app = self.app(URL + "A\ninvalid")
        real_sync = config.sync_directories
        healthy = False

        def sync(path):
            if not healthy:
                raise OSError("test directory fsync failure")
            real_sync(path)

        with patch.object(config, "sync_directories", side_effect=sync):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(pilot, app)
                await self.submit(pilot)
                await self.wait_for(pilot, lambda: bool(app.session.index.error) and not app.screen.saving)
                self.assertIn("durability uncertain", app.session.index.error)
                self.assertEqual(app.screen.query_one(TextArea).text, URL + "A\ninvalid")
                self.assertFalse(app.session.media_allowed)
                healthy = True
                self.assertTrue(await pilot.click("#add-recover"))
                await self.wait_for(pilot, lambda: app.session.index.editable and app.recovery_draft is None)
                self.assertEqual(app.screen.query_one(TextArea).text, "invalid")
                self.assertEqual([row.url for row in self.disk.read()], [URL + "A"])
                self.assertFalse(app.session.busy)

    async def test_log_is_bounded_and_recent_external_retry_uses_worker(self):
        store = jobs.JobStore(self.paths.state)
        entry = self.entry("External")
        job = jobs.Job.create(entry.id, Settings("single", "0", False, self.output),
                              [jobs.make_item(1, "External", "Literal [bold] title", False)])
        store.save(job)
        with store.claim(job.id):
            pass
        store.logs.mkdir()
        (store.logs / f"{job.id}.log").write_text("\n".join(f"line-{n} [bold]" for n in range(250)))
        app = self.app()
        with patch.object(runner, "retry", return_value=Result(failed=1)) as retry:
            async with app.run_test(size=(100, 24)) as pilot:
                await self.ready(pilot, app)
                await pilot.click("#recent")
                await self.wait_for(pilot, lambda: isinstance(app.screen, RecentJobs) and app.session.external.get(job.id))
                await pilot.click("#recent-details")
                await self.wait_for(pilot, lambda: isinstance(app.screen, Details) and app.screen.ready)
                app.screen.query_one(TabbedContent).active = "log-pane"
                await self.wait_for(pilot, lambda: app.screen.log_loaded)
                log_text = str(app.screen.query_one("#log-text").content)
                self.assertIn("Older text omitted", log_text)
                self.assertIn("line-249 [bold]", log_text)
                self.assertNotIn("line-49 [bold]", log_text)
                await pilot.click("#details-retry")
                await self.wait_for(pilot, lambda: retry.await_count == 1 and not app.session.busy)
                self.assertEqual(retry.call_args.args[0], job.id)
                self.assertEqual(app.session.index.rows, [])

    async def test_failed_add_before_replacement_keeps_previous_queue(self):
        existing = self.entry("A")
        self.disk.save([existing])
        app = self.app(URL + "B")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            original = self.disk.path.read_bytes()
            with patch.object(self.disk, "save", side_effect=OSError("No space before replacing queue")):
                await self.submit(pilot)
                await self.wait_for(pilot, lambda: "Save rejected" in app.session.index.notice)
            self.assertFalse(app.session.index.error)
            self.assertEqual(self.disk.path.read_bytes(), original)
            self.assertEqual(app.screen.query_one(TextArea).text, URL + "B")
            self.assertTrue(app.session.index.runnable)

    async def test_quit_modal_stay_then_stop_and_quit_preserves_pending(self):
        row, pending = [self.entry(name) for name in "AB"]
        self.disk.save([row, pending])
        app = self.app()

        async def download(*_args, decide, **_kwargs):
            try:
                await decide(Decision("Download the entire playlist (52 entries)?", (("y", "Yes"), ("n", "No")), "n"))
            except asyncio.CancelledError:
                raise runner.AttemptStopped(planned=False) from None

        with patch.object(runner, "download", side_effect=download) as operation:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(pilot, app)
                await pilot.click("#primary")
                await self.wait_for(pilot, lambda: isinstance(app.screen, RunnerPrompt) and app.screen.ready)
                prompt = app.screen
                self.assertTrue(await pilot.click("#modal-quit"))
                self.assertEqual(app.focused.id, "cancel")
                await pilot.press("enter")
                self.assertIs(app.screen, prompt)
                self.assertTrue(app.session.busy)
                await pilot.press("ctrl+q")
                await pilot.click("#confirm")
                async with asyncio.timeout(5):
                    while app.return_value is None:
                        await asyncio.sleep(0.02)
                self.assertEqual(app.return_value, 0)
                operation.assert_awaited_once()
                self.assertEqual([entry.status for entry in self.disk.read()], ["pending", "pending"])

    async def test_save_uncertainty_while_runner_decision_remains_interactive(self):
        await self.check_runner_prompt_recovery(answer="n", expected="cancelled")

    async def test_save_uncertainty_buffers_confirmed_media_result(self):
        await self.check_runner_prompt_recovery(answer="y", expected="failed")

    async def check_runner_prompt_recovery(self, *, answer, expected):
        import threading

        original_row, following = [self.entry(name) for name in "AB"]
        self.disk.save([original_row, following])
        before_prompt = asyncio.Event()
        app = self.app(URL + "New")
        sync = config.sync_directories
        pending_add = asyncio.Event()
        finish_add = threading.Event()
        loop = asyncio.get_running_loop()

        def interrupt_sync(path):
            if path == self.paths.state and not finish_add.is_set() and self.disk.path.read_bytes().count(b'"id"') == 3:
                loop.call_soon_threadsafe(pending_add.set)
                if not finish_add.wait(5):
                    raise RuntimeError("Test did not release the Add save")
                raise OSError("Add directory sync failed after replacement")
            sync(path)

        async def download(*_args, job_id, decide, **_kwargs):
            await before_prompt.wait()
            response = await decide(Decision("Download the entire playlist (52 entries)?", (("y", "Yes"), ("n", "No")), "n"))
            if response == "n":
                return Result(declined=True)
            store = jobs.JobStore(self.paths.state)
            plan = jobs.Job.create(job_id, Settings("playlist", "0", False, self.output / "Playlist"),
                                   [jobs.make_item(1, "A", "Track", True)], enqueued_base=str(self.output))
            with store.claim(job_id, new=True):
                store.save(plan)
            return Result(failed=1, job_id=job_id, diagnostics=("Fixture media failure",))

        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            with patch.object(runner, "download", side_effect=download) as operation:
                with patch.object(config, "sync_directories", side_effect=interrupt_sync):
                    await pilot.click("#primary")
                    await self.wait_for(pilot, lambda: app.session.active_id == original_row.id)
                    await self.submit(pilot)
                    try:
                        await self.wait_for(pilot, pending_add.is_set)
                        before_prompt.set()
                        await self.wait_for(pilot, lambda: isinstance(app.screen, RunnerPrompt) and app.screen.ready)
                    finally:
                        finish_add.set()
                    await self.wait_for(pilot, lambda: bool(app.session.index.error))
                    self.assertTrue(await pilot.click("#choice-0" if answer == "y" else "#choice-1"))
                    await self.wait_for(pilot, lambda: not app.session.busy)
                    operation.assert_awaited_once()
                    buffered = app.session.index.buffer[original_row.id]
                    self.assertEqual(buffered.status, expected)
                    if answer == "n":
                        self.assertEqual(buffered.diagnostic, "Playlist download declined")
                    self.assertTrue(await pilot.click("#add-recover"))
                    await self.wait_for(pilot, lambda: app.session.index.editable and app.recovery_draft is None)
                    self.assertEqual([row.status for row in self.disk.read()], [expected, "pending", "pending"])
                    self.assertFalse(app.session.busy)
