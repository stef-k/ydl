"""Exercise Recent jobs against the queue and retry lifecycle."""

import asyncio
from dataclasses import replace
from threading import Event as ThreadEvent
from unittest.mock import patch

from textual.widgets import Button, DataTable

from ydl import jobs, runner
from ydl.models import Event, Result, Settings
from ydl.tui_dialogs import Confirm
from ydl.tui_jobs import Details, RecentJobs

from tui_pilot import PilotCase


class RecentJobsTests(PilotCase):
    async def test_recent_job_is_verified_when_its_queue_row_is_removed(self):
        row = replace(self.entry("Removed&list=SavedPlaylist"), status="failed")
        store = jobs.JobStore(self.paths.state)
        settings = Settings("playlist", "0", False, self.output / "Saved playlist")
        items = [jobs.make_item(1, "Removed", "Saved track", False),
                 jobs.make_item(2, "Second", "Second track", False)]
        with store.claim(row.id, new=True):
            store.save(jobs.Job.create(row.id, settings, items, enqueued_base=row.output))
        self.disk.save([row])
        saving, release = ThreadEvent(), ThreadEvent()
        save = self.disk.save

        def finish_removal(rows):
            if not rows:
                saving.set()
                if not release.wait(10):
                    raise TimeoutError("Test did not release the removal save")
            save(rows)

        app = self.app()
        with patch.object(self.disk, "save", side_effect=finish_removal):
            async with app.run_test(size=(100, 30)) as pilot:
                try:
                    await self.ready(pilot, app)
                    app.dashboard.query_one("#queue").focus()
                    await pilot.press("delete")
                    await self.wait_for(pilot, lambda: isinstance(app.screen, Confirm))
                    self.assertTrue(await pilot.click("#confirm"))
                    await self.wait_for(pilot, saving.is_set)
                    self.assertTrue(await pilot.click("#recent"))
                    await self.wait_for(pilot, lambda: isinstance(app.screen, RecentJobs) and not app.screen.is_loading)
                    table = app.screen.query_one(DataTable)
                    self.assertEqual(table.get_cell(row.id, "status").plain, "Failed")
                finally:
                    release.set()
                await self.wait_for(pilot, lambda: "Incomplete" in self.screen_text(app))
                self.assertFalse(app.screen.query_one("#recent-retry", Button).disabled)
                self.assertEqual(self.disk.read(), [])

                finish_retry = asyncio.Event()
                progress = Event("downloading", title="Saved track", position=1, total=2, percent=50)

                async def retry(job_id, *_args, emit, **_kwargs):
                    self.assertEqual(job_id, row.id)
                    emit(progress)
                    await finish_retry.wait()
                    return Result(failed=1)

                with patch.object(runner, "retry", side_effect=retry):
                    try:
                        self.assertTrue(await pilot.click("#recent-retry"))
                        await self.wait_for(pilot, lambda: app.session.events.get(row.id) == progress)
                        await pilot.press("escape")
                        self.assertIn("Downloading · Playlist 1/2", self.screen_text(app))
                        self.assertIn("Queue · 0 URLs · 0 Pending", self.screen_text(app))
                    finally:
                        finish_retry.set()
                        await self.wait_for(pilot, lambda: not app.session.busy)

    async def test_recent_job_explains_a_plan_that_disappears_during_verification(self):
        row_id = self.entry("Missing").id
        store = jobs.JobStore(self.paths.state)
        plan_path = store.path(row_id)
        with store.claim(row_id, new=True):
            store.save(jobs.Job.create(row_id, Settings("single", "0", False, self.output),
                                      [jobs.make_item(1, "Missing", "Saved track", False)]))
        inspect = jobs.inspect_job

        async def move_plan_before_read(store, job_id, **kwargs):
            plan_path.rename(self.root / "moved-plan.json")
            return await inspect(store, job_id, **kwargs)

        app = self.app()
        with patch.object(jobs, "inspect_job", side_effect=move_plan_before_read):
            async with app.run_test(size=(105, 30)) as pilot:
                await self.ready(pilot, app)
                self.assertTrue(await pilot.click("#recent"))
                await self.wait_for(pilot, lambda: "Needs attention" in self.screen_text(app))
                note = str(app.screen.query_one("#recent-note").content)
                self.assertIn(str(plan_path), note)
                self.assertIn("Restore it and reopen Recent jobs", note)
                self.assertTrue(app.screen.query_one("#recent-retry", Button).disabled)
                self.assertTrue(await pilot.click("#recent-details"))
                await self.wait_for(pilot, lambda: isinstance(app.screen, Details) and app.screen.ready)
                self.assertIn(note, str(app.screen.query_one("#details-info").content))

    async def test_queue_linked_retry_does_not_compete_with_recent_verification(self):
        row = replace(self.entry("Linked"), status="failed")
        self.disk.save([row])
        store = jobs.JobStore(self.paths.state)
        with store.claim(row.id, new=True):
            store.save(jobs.Job.create(row.id, Settings("single", "0", False, self.output),
                                      [jobs.make_item(1, "Linked", "Track", False)],
                                      enqueued_base=row.output))
        inspect = jobs.inspect_job
        release = asyncio.Event()

        async def verify(store, row_id, *, enqueued=None):
            if enqueued is None:
                with store.observe(row_id):
                    await release.wait()
            return await inspect(store, row_id, enqueued=enqueued)

        app = self.app()
        with patch.object(jobs, "inspect_job", side_effect=verify), patch.object(
                runner, "execute", return_value=Result(failed=1)) as execute:
            async with app.run_test(size=(100, 30)) as pilot:
                try:
                    await self.ready(pilot, app)
                    self.assertTrue(await pilot.click("#recent"))
                    await self.wait_for(pilot, lambda: isinstance(app.screen, RecentJobs) and not app.screen.is_loading)
                    self.assertTrue(await pilot.click("#recent-retry"))
                    await self.wait_for(pilot, lambda: not app.session.busy)
                    self.assertEqual(app.session.index.view(row.id).status, "Failed")
                    execute.assert_awaited_once()
                finally:
                    release.set()
                    await pilot.pause()

    async def test_recovery_releases_recent_job_lock_before_retry(self):
        row = replace(self.entry("Recovered"), status="failed")
        store = jobs.JobStore(self.paths.state)
        with store.claim(row.id, new=True):
            store.save(jobs.Job.create(row.id, Settings("single", "0", False, self.output),
                                      [jobs.make_item(1, "Recovered", "Track", False)],
                                      enqueued_base=row.output))
        self.disk.path.write_text("invalid queue JSON")
        inspecting, cancelling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        inspect = jobs.inspect_job

        async def verify(store, row_id, *, enqueued=None):
            if enqueued is None:
                with store.observe(row_id):
                    inspecting.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        cancelling.set()
                        await release.wait()
                        raise
            return await inspect(store, row_id, enqueued=enqueued)

        app = self.app()
        with patch.object(jobs, "inspect_job", side_effect=verify), patch.object(
                runner, "execute", return_value=Result(failed=1)) as execute:
            async with app.run_test(size=(100, 30)) as pilot:
                try:
                    await self.wait_for(pilot, lambda: app.session.can_recover)
                    self.assertTrue(await pilot.click("#recent"))
                    await self.wait_for(pilot, inspecting.is_set)
                    await pilot.press("escape")
                    self.disk.save([row])
                    self.assertTrue(await pilot.click("#recover"))
                    await self.wait_for(pilot, cancelling.is_set)
                    self.assertTrue(app.dashboard.query_one("#retry-failed", Button).disabled)
                finally:
                    release.set()
                    await pilot.pause()
                await self.ready(pilot, app)
                self.assertEqual(app.session.index.view(row.id).status, "Failed")
                self.assertFalse(app.session.busy)
                self.assertTrue(await pilot.click("#retry-failed"))
                await self.wait_for(pilot, lambda: not app.session.busy)
                self.assertEqual(app.session.index.view(row.id).status, "Failed")
                execute.assert_awaited_once()

    async def test_reopening_recent_during_retry_settlement_preserves_result(self):
        row_id = self.entry("External").id
        store = jobs.JobStore(self.paths.state)
        with store.claim(row_id, new=True):
            store.save(jobs.Job.create(row_id, Settings("single", "0", False, self.output),
                                      [jobs.make_item(1, "External", "Track", False)]))
        inspect = jobs.inspect_job
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause_settlement(*args, **kwargs):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await inspect(*args, **kwargs)

        app = self.app()
        failure = Result(failed=1, diagnostics=("Track unavailable",))
        async with app.run_test(size=(100, 30)) as pilot:
            await self.ready(pilot, app)
            self.assertTrue(await pilot.click("#recent"))
            await self.wait_for(pilot, lambda: isinstance(app.screen, RecentJobs)
                                and app.session.external.get(row_id) is not None)
            with patch.object(runner, "retry", return_value=failure), patch.object(
                    jobs, "inspect_job", side_effect=pause_settlement):
                try:
                    self.assertTrue(await pilot.click("#recent-retry"))
                    await self.wait_for(pilot, entered.is_set)
                    await pilot.press("escape")
                    self.assertTrue(await pilot.click("#recent"))
                    await self.wait_for(pilot, lambda: isinstance(app.screen, RecentJobs) and not app.screen.is_loading)
                    self.assertTrue(await pilot.click("#recent-details"))
                    await self.wait_for(pilot, lambda: isinstance(app.screen, Details) and app.screen.ready)
                finally:
                    release.set()
                await self.wait_for(pilot, lambda: not app.session.busy)
                text = self.screen_text(app)
                self.assertRegex(text, r"Verified +0/1")
                self.assertRegex(text, r"Failed +1")
                self.assertIn("Track unavailable", text)
