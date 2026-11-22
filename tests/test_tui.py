"""Exercise terminal interactions through the installed Textual frontend."""

import asyncio
import hashlib
import threading
from dataclasses import replace
from unittest.mock import patch

from textual.events import MouseScrollDown
from textual.widgets import Button, Input, RadioSet, Select, TabbedContent, TextArea

from ydl import config, jobs, media, runner
from ydl.models import Decision, Event, Result, Settings, YdlError
from ydl.tui_dialogs import AddURLs, Browse, Confirm, HelpPanel, RunnerPrompt, SettingsPanel
from ydl.tui_jobs import Details, RecentJobs, RowMenu

from tui_pilot import PilotCase, URL


class PilotTests(PilotCase):
    async def test_prefill_batch_validation_and_restore(self):
        draft = "https://youtu.be/A?t=2\n\nnot-a-url\nhttps://www.youtube.com/watch?v=A\nhttps://youtu.be/B"
        app = self.app(draft)
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            self.assertEqual(app.focused.id, "draft")
            self.assertEqual(app.focused.text, draft)
            self.assertEqual(app.session.index.rows, [])
            await pilot.press("tab")
            self.assertEqual(app.focused.id, "add-urls")
            await pilot.press("shift+tab")
            self.assertEqual(app.focused.id, "draft")
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
            self.assertIn("Queue restored · 2 Pending", str(reopened.screen.query_one("#notice").render()))

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
        entries = [self.entry(f"LongTitle{i}") for i in range(25)]
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
            await pilot.double_click("#queue", offset=(6, 1))
            self.assertIsInstance(app.screen, Details)
            self.assertEqual(len(app.screen_stack), 2)
            await pilot.press("escape")
            await pilot.press("f2")
            self.assertIsInstance(app.screen, RowMenu)
            await pilot.click("#down")
            await self.wait_for(pilot, lambda: self.disk.read()[1].id == entries[0].id)
            self.assertEqual(app.selected, entries[0].id)
            await pilot._post_mouse_events([MouseScrollDown], queue, offset=(5, 2))
            await self.wait_for(pilot, lambda: queue.scroll_y > 0)
            self.assertEqual(app.selected, entries[0].id)
            await pilot.resize_terminal(112, 26)
            await self.wait_for(pilot, lambda: len(queue.columns) == 7)
            await pilot.click("#help")
            self.assertIsInstance(app.screen, HelpPanel)
            await pilot.press("escape")
            self.assertEqual(app.focused.id, "help")
            await pilot.resize_terminal(150, 38)
            await pilot.pause()
            queue.focus()
            await pilot.press("ctrl+end")
            await self.wait_for(pilot, lambda: app.selected == entries[-1].id and queue.scroll_y > 0)
            await pilot.press("f2")
            self.assertIsInstance(app.screen, RowMenu)
            self.assertEqual(app.screen.row_id, entries[-1].id)
            await pilot.press("escape")
            self.assertIs(app.focused, queue)

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

    async def test_successful_add_settles_under_runner_prompt(self):
        for rejected in ("", "not-a-url"):
            with self.subTest(rejected=rejected):
                await self.check_successful_add_under_prompt(rejected)

    async def check_successful_add_under_prompt(self, rejected):
        original = self.entry("A")
        self.disk.save([original])
        draft = "\n".join(filter(None, (URL + "New", rejected)))
        app = self.app(draft)
        before_prompt, pending_add = asyncio.Event(), asyncio.Event()
        finish_add = threading.Event()
        sync = config.sync_directories
        loop = asyncio.get_running_loop()
        answers = []

        def hold_sync(path):
            if path == self.paths.state and not finish_add.is_set() and self.disk.path.read_bytes().count(b'"id"') == 2:
                loop.call_soon_threadsafe(pending_add.set)
                if not finish_add.wait(5):
                    raise RuntimeError("Test did not release the Add save")
            sync(path)

        async def download(*_args, job_id, decide, **_kwargs):
            if job_id == original.id:
                await before_prompt.wait()
                answers.append(await decide(Decision("Download the entire playlist (52 entries)?",
                                                     (("y", "Yes"), ("n", "No")), "n")))
                return Result(declined=True)
            return Result(saved=1)

        with patch.object(runner, "download", side_effect=download), patch.object(config, "sync_directories", hold_sync):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(pilot, app)
                await pilot.click("#primary")
                await self.wait_for(pilot, lambda: app.session.active_id == original.id)
                await self.submit(pilot)
                add = app.screen
                try:
                    await self.wait_for(pilot, pending_add.is_set)
                    before_prompt.set()
                    await self.wait_for(pilot, lambda: isinstance(app.screen, RunnerPrompt)
                                        and getattr(app.screen, "ready", False))
                    prompt = app.screen
                finally:
                    finish_add.set()
                try:
                    await self.wait_for(pilot, lambda: not add.saving)
                    self.assertIs(app.screen, prompt)
                    self.assertEqual(app.focused.id, "choice-1")
                    self.assertTrue(await pilot.click("#choice-1"))
                    await self.wait_for(pilot, lambda: not app.session.busy)
                    self.assertEqual(answers, ["n"])
                    self.assertEqual([row.url for row in self.disk.read()], [original.url, URL + "New"])
                    if rejected:
                        self.assertIs(app.screen, add)
                        self.assertEqual(app.focused.id, "batch-draft")
                        self.assertEqual(app.focused.text, rejected)
                        self.assertIn("Line 2", str(add.query_one("#add-error").render()))
                        await pilot.press("escape")
                    self.assertIs(app.screen, app.dashboard)
                    self.assertEqual(app.dashboard.query_one("#draft", TextArea).text, rejected)
                    self.assertEqual(app.focused.id, "add-urls")
                finally:
                    await app.session.stop()

    async def check_runner_prompt_recovery(self, *, answer, expected):
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

    async def test_entire_playlist_invokes_core_size_confirmation_after_scope_choice(self):
        row = self.entry("A&list=LargePlaylist")
        self.disk.save([row])
        app = self.app()
        deps = runner.Dependencies([], "offline-ffmpeg", "offline-ffprobe")
        metadata = {"playlist_count": 60, "entries": [{"id": "A", "title": "Track A"}]}
        with patch.object(runner, "prepare", return_value=deps):
            with patch.object(media, "metadata", return_value=metadata) as lookup:
                async with app.run_test(size=(80, 24)) as pilot:
                    await self.ready(pilot, app)
                    await pilot.click("#primary")
                    await self.wait_for(pilot, lambda: isinstance(app.screen, RunnerPrompt) and app.screen.ready)
                    lookup.assert_not_called()
                    await pilot.press("down", "space")
                    self.assertEqual(app.screen.query_one(RadioSet).pressed_index, 1)
                    await pilot.click("#scope-confirm")
                    await self.wait_for(pilot, lambda: isinstance(app.screen, RunnerPrompt)
                                        and app.screen.ready and not app.screen.scope)
                    self.assertIn("60 entries", app.screen.decision.question)
                    self.assertEqual(app.focused.id, "choice-1")
                    await pilot.press("enter")
                    await self.wait_for(pilot, lambda: not app.session.busy)
                    lookup.assert_awaited_once()
                    self.assertTrue(lookup.call_args.kwargs["playlist"])
                    self.assertEqual(self.disk.read()[0].diagnostic, "Playlist download declined")
                    self.assertFalse(app.session.store.path(row.id).exists())

    async def test_clear_completed_and_remove_do_not_touch_job_files(self):
        finished, stale = self.entry("saved"), self.entry("lost")
        stale = replace(stale, status="active")
        store = jobs.JobStore(self.paths.state)
        item = jobs.make_item(1, "saved", "Saved song", False)
        published = self.output / (item.stem + ".mp3")
        published.write_bytes(b"saved MP3")
        item.outputs["mp3"] = {"name": published.name, "sha256": hashlib.sha256(published.read_bytes()).hexdigest()}
        plan = jobs.Job.create(finished.id, Settings("single", "0", False, self.output), [item])
        with store.claim(finished.id, new=True):
            store.save(plan)
        store.logs.mkdir()
        log = store.logs / f"{plan.id}.log"
        log.write_text("Kept log")
        self.disk.save([finished, stale])
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            self.assertEqual(app.session.index.view(stale.id).status, "Needs attention")
            self.assertTrue(app.can_row("recheck", stale.id))
            await pilot.click("#clear-completed")
            await pilot.press("enter")
            self.assertEqual(len(self.disk.read()), 2)
            await pilot.click("#clear-completed")
            await pilot.click("#confirm")
            await self.wait_for(pilot, lambda: len(app.session.index.rows) == 1)
            self.assertEqual(app.selected, stale.id)
            await pilot.press("delete")
            await pilot.click("#confirm")
            await self.wait_for(pilot, lambda: not app.session.index.rows)
        self.assertTrue(store.path(plan.id).exists())
        self.assertEqual(published.read_bytes(), b"saved MP3")
        self.assertEqual(log.read_text(), "Kept log")

    async def test_selected_retry_and_shift_r_use_the_same_failed_row_gates(self):
        first, second = [replace(self.entry(name), status="failed_before_plan") for name in "AB"]
        self.disk.save([first, second])
        app = self.app()
        with patch.object(runner, "download", side_effect=YdlError("Offline fixture failure")) as attempt:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(pilot, app)
                app.dashboard.query_one("#queue").focus()
                await pilot.press("down", "r")
                await self.wait_for(pilot, lambda: attempt.await_count == 1 and not app.session.busy)
                self.assertEqual(attempt.await_args.kwargs["job_id"], second.id)
                await pilot.press("R")
                await self.wait_for(pilot, lambda: attempt.await_count == 3 and not app.session.busy)
                self.assertEqual([call.kwargs["job_id"] for call in attempt.await_args_list],
                                 [second.id, first.id, second.id])
