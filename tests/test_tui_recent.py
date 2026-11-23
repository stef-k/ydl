"""Exercise Recent jobs against the queue and retry lifecycle."""

import asyncio
from dataclasses import replace
from unittest.mock import patch

from textual.widgets import Button

from ydl import jobs, runner
from ydl.models import Result, Settings
from ydl.tui_jobs import Details, RecentJobs

from tui_pilot import PilotCase


class RecentJobsTests(PilotCase):
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
