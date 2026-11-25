"""Exercise Recent jobs against the queue and retry lifecycle."""

import asyncio
import hashlib
import os
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
    async def test_open_saved_outputs_from_queue_and_recent_jobs_after_clear(self):
        row = replace(self.entry("Track", keep_video=True), status="complete")
        item = jobs.Item(1, "Track", "Saved song", "Saved song [Track]")
        audio = self.output / f"{item.stem}.mp3"
        video = self.output / f"{item.stem}.mkv"
        for kind, path in (("mp3", audio), ("video", video)):
            path.write_bytes(kind.encode())
            item.outputs[kind] = {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        item.video_target = video.name
        store = jobs.JobStore(self.paths.state)
        settings = Settings("single", "0", True, self.output)
        with store.claim(row.id, new=True):
            store.save(jobs.Job.create(row.id, settings, [item], enqueued_base=row.output))
        self.disk.save([row])

        opened = self.root / "opened"
        gio = self.root / "gio"
        gio.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$YDL_TEST_OPENED"\n')
        gio.chmod(0o700)
        app = self.app()
        with patch.dict(os.environ, PATH=f"{self.root}:{os.environ['PATH']}", YDL_TEST_OPENED=str(opened)):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(pilot, app)
                app.dashboard.query_one("#queue").focus()
                await pilot.press("d")
                self.assertTrue(await pilot.click("#open-folder"))
                await self.wait_for(pilot, lambda: opened.exists() and len(opened.read_text().splitlines()) == 2)
                self.assertTrue(await pilot.click("#open-mp3"))
                await self.wait_for(pilot, lambda: len(opened.read_text().splitlines()) == 4)
                await pilot.press("escape")
                self.assertTrue(await pilot.click("#clear-completed"))
                self.assertTrue(await pilot.click("#confirm"))
                await self.wait_for(pilot, lambda: not app.session.index.rows)
                self.assertTrue(await pilot.click("#recent"))
                await self.wait_for(pilot, lambda: app.session.external.get(row.id))
                self.assertTrue(await pilot.click("#recent-details"))
                open_video = app.screen.query_one("#open-video", Button)
                open_video.focus()
                await pilot.press("enter")
                await self.wait_for(pilot, lambda: len(opened.read_text().splitlines()) == 6)
                expected = ["open", str(self.output), "open", str(audio), "open", str(video)]
                self.assertEqual(opened.read_text().splitlines(), expected)

                video.unlink()
                await self.wait_for(pilot, lambda: not open_video.has_class("-active"))
                with patch.object(app, "notify", wraps=app.notify) as notify:
                    await pilot.press("enter")
                    await self.wait_for(pilot, lambda: notify.called)
                    self.assertIn(str(video), notify.call_args.args[0])
                    self.assertIn("Retry selected job", notify.call_args.args[0])
                self.assertEqual(opened.read_text().splitlines(), expected)
                self.assertIn("Incomplete", self.screen_text(app))
                self.assertFalse(open_video.display)
                self.assertFalse(app.screen.query_one("#details-retry", Button).disabled)
                await pilot.press("escape")
                self.assertIn("Incomplete", self.screen_text(app))
                self.assertFalse(app.screen.query_one("#recent-retry", Button).disabled)

    async def test_confirmed_quit_waits_for_open_audio_verification(self):
        complete, active, pending = (self.entry(name) for name in ("Complete", "Active", "Pending"))
        complete = replace(complete, status="complete")
        self.disk.save([complete, active, pending])
        item = jobs.make_item(1, "Complete", "Saved song", False)
        audio = self.output / f"{item.stem}.mp3"
        audio.write_bytes(b"saved audio")
        item.outputs["mp3"] = {"name": audio.name, "sha256": hashlib.sha256(audio.read_bytes()).hexdigest()}
        store = jobs.JobStore(self.paths.state)
        with store.claim(complete.id, new=True):
            store.save(jobs.Job.create(complete.id, Settings("single", "0", False, self.output), [item],
                                      enqueued_base=complete.output))
        verifying, cancelling, release = ThreadEvent(), ThreadEvent(), ThreadEvent()
        sha256 = jobs._sha256

        def slow_hash(path, *, stop):
            verifying.set()
            while not release.wait(0.01):
                if stop.is_set():
                    cancelling.set()
            return sha256(path, stop=stop)

        async def download(*_args, **_kwargs):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                raise runner.AttemptStopped(planned=False) from None

        app = self.app()
        with patch.object(runner, "download", side_effect=download) as operation:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(pilot, app)
                self.assertTrue(await pilot.click("#primary"))
                await self.wait_for(pilot, lambda: app.session.active_id == active.id)
                app.dashboard.query_one("#queue").focus()
                await pilot.press("ctrl+home", "d")
                with patch.object(jobs, "_sha256", side_effect=slow_hash), patch(
                        "ydl.tui_jobs.open_in_desktop") as launch:
                    try:
                        self.assertTrue(await pilot.click("#open-mp3"))
                        await self.wait_for(pilot, verifying.is_set)
                        await pilot.press("escape", "ctrl+q")
                        self.assertIsInstance(app.screen, Confirm)
                        self.assertTrue(await pilot.click("#confirm"))
                        async with asyncio.timeout(5):
                            while app.return_code is None and (app.session.busy or not cancelling.is_set()):
                                await asyncio.sleep(0.02)
                        self.assertIsNone(app.return_code)
                        self.assertTrue(cancelling.is_set())
                    finally:
                        release.set()
                    async with asyncio.timeout(5):
                        while app.return_code is None:
                            await asyncio.sleep(0.02)
                    launch.assert_not_called()
        self.assertEqual(app.return_code, 0)
        operation.assert_awaited_once()
        with store.claim(complete.id):
            kept, stopped, untouched = self.disk.read()
            self.assertEqual(kept, complete)
            self.assertEqual(replace(stopped, diagnostic=""), active)
            self.assertEqual(untouched, pending)

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
