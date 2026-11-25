"""Exercise changed download targets through the Job Details Open actions."""

import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
from unittest.mock import patch

from textual.widgets import Button, DataTable

from ydl import jobs, runner, tui_jobs
from ydl.models import Settings
from ydl.tui_dialogs import Confirm

from tui_pilot import PilotCase


class OpenTests(PilotCase):
    def save_job(self, *, playlist=False, keep_video=False):
        row_id = self.entry("Track").id
        folder = self.output / "Saved playlist [Playlist]" if playlist else self.output
        folder.mkdir(exist_ok=True)
        item = jobs.make_item(1, "Track", "Saved song", playlist)
        audio = folder / f"{item.stem}.mp3"
        video = folder / f"{item.stem}.mkv"
        files = {"mp3": audio}
        if keep_video:
            files["video"] = video
            item.video_target = video.name
        for kind, path in files.items():
            path.write_bytes(kind.encode())
            item.outputs[kind] = {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        settings = Settings("playlist" if playlist else "single", "0", keep_video, folder)
        store = jobs.JobStore(self.paths.state)
        with store.claim(row_id, new=True):
            store.save(jobs.Job.create(row_id, settings, [item]))
        return folder, audio, video

    async def show_recent_details(self, pilot, app):
        await self.ready(pilot, app)
        self.assertTrue(await pilot.click("#recent"))
        table = app.screen.query_one("#recent-table", DataTable)
        await self.wait_for(pilot, lambda: table.row_count and str(table.get_row_at(0)[1]) == "Complete")
        self.assertTrue(await pilot.click("#recent-details"))

    async def test_open_folder_blocks_replacement_directory(self):
        folder, audio, _ = self.save_job(playlist=True)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.show_recent_details(pilot, app)
            folder.rename(self.root / "original")
            folder.mkdir()
            self.assertFalse(audio.exists())
            with patch("ydl.tui_jobs.open_in_desktop") as launch, patch.object(app, "notify", wraps=app.notify) as notify:
                self.assertTrue(await pilot.click("#open-folder"))
                await self.wait_for(pilot, lambda: launch.called or notify.called)
                launch.assert_not_called()
                self.assertIn("folder has changed", notify.call_args.args[0])
            self.assertIn("Incomplete", self.screen_text(app))
            self.assertFalse(app.screen.query_one("#details-retry", Button).disabled)

    async def test_open_folder_refreshes_recent_job_when_a_track_is_removed(self):
        _, audio, _ = self.save_job(playlist=True)
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.show_recent_details(pilot, app)
            audio.unlink()
            with patch.object(tui_jobs.subprocess, "Popen") as launch, patch.object(
                    app, "notify", wraps=app.notify) as notify:
                self.assertTrue(await pilot.click("#open-folder"))
                await self.wait_for(pilot, lambda: launch.called or notify.called)
                launch.assert_not_called()
                self.assertIn("Retry selected job", notify.call_args.args[0])
            self.assertIn("Incomplete", self.screen_text(app))
            self.assertFalse(app.screen.query_one("#details-retry", Button).disabled)
            await pilot.press("escape")
            self.assertIn("Incomplete", self.screen_text(app))
            self.assertFalse(app.screen.query_one("#recent-retry", Button).disabled)

    async def test_open_folder_blocks_a_track_removed_during_dispatch_checks(self):
        await self.check_playlist_dispatch_change(lambda path: path.unlink(), "Incomplete", "Retry selected job")

    async def test_open_folder_blocks_a_track_overwritten_during_dispatch_checks(self):
        await self.check_playlist_dispatch_change(lambda path: path.write_bytes(b"new"), "Needs attention",
                                                  "Move it aside")

    async def check_playlist_dispatch_change(self, change, status, diagnostic):
        folder, audio, _ = self.save_job(playlist=True)
        store = jobs.JobStore(self.paths.state)
        job_id, = store.recent_ids()
        with store.claim(job_id):
            job = store.load(job_id)
            item = jobs.make_item(2, "Next", "Next song", True)
            later = folder / f"{item.stem}.mp3"
            later.write_bytes(b"second track")
            item.outputs["mp3"] = {"name": later.name, "sha256": hashlib.sha256(later.read_bytes()).hexdigest()}
            job.items.append(item)
            store.save(job)

        checking, release = Event(), Event()
        output_stamp, dispatch_problem = jobs.output_stamp, tui_jobs.dispatch_problem

        def pause_later_track(path):
            if path == later:
                checking.set()
                if not release.wait(5):
                    raise TimeoutError("Dispatch verification did not resume")
            return output_stamp(path)

        def pause_dispatch(*args, **kwargs):
            with patch.object(jobs, "output_stamp", side_effect=pause_later_track):
                return dispatch_problem(*args, **kwargs)

        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.show_recent_details(pilot, app)
            with patch.object(tui_jobs, "dispatch_problem", side_effect=pause_dispatch), patch.object(
                    tui_jobs.subprocess, "Popen") as launch, patch.object(
                    app, "notify", wraps=app.notify) as notify:
                try:
                    self.assertTrue(await pilot.click("#open-folder"))
                    await self.wait_for(pilot, checking.is_set)
                    change(audio)
                finally:
                    release.set()
                await self.wait_for(pilot, lambda: launch.called or notify.called)
                launch.assert_not_called()
                self.assertIn(diagnostic, notify.call_args.args[0])
            self.assertIn(status, self.screen_text(app))
            self.assertEqual(app.screen.query_one("#details-retry", Button).disabled, status != "Incomplete")
            await pilot.press("escape")
            self.assertIn(status, self.screen_text(app))
            self.assertEqual(app.screen.query_one("#recent-retry", Button).disabled, status != "Incomplete")

    async def test_open_audio_blocks_a_file_changed_while_checking_retained_video(self):
        folder, audio, video = self.save_job(keep_video=True)
        app = self.app()
        checking_video, release = Event(), Event()
        sha256 = jobs._sha256

        def pause_video(path, *, stop):
            if path == video:
                checking_video.set()
                if not release.wait(5):
                    raise TimeoutError("Retained video verification did not resume")
            return sha256(path, stop=stop)

        async with app.run_test(size=(80, 24)) as pilot:
            await self.show_recent_details(pilot, app)
            with patch.object(jobs, "_sha256", side_effect=pause_video), patch(
                    "ydl.tui_jobs.open_in_desktop") as launch, patch.object(app, "notify", wraps=app.notify) as notify:
                try:
                    self.assertTrue(await pilot.click("#open-mp3"))
                    await self.wait_for(pilot, checking_video.is_set)
                    replacement = folder / "replacement"
                    replacement.write_bytes(b"changed audio")
                    replacement.replace(audio)
                finally:
                    release.set()
                await self.wait_for(pilot, lambda: launch.called or notify.called)
                launch.assert_not_called()
                self.assertIn(str(audio), notify.call_args.args[0])
            self.assertIn("Needs attention", self.screen_text(app))

    async def test_open_audio_blocks_a_file_changed_while_dispatch_waits(self):
        _, audio, _ = self.save_job()
        app = self.app()
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        hashing, release_hash = Event(), Event()
        occupied, release_worker = Event(), Event()
        sha256 = jobs._sha256

        def pause_hash(path, *, stop):
            hashing.set()
            if not release_hash.wait(5):
                raise TimeoutError("Receipt verification did not resume")
            return sha256(path, stop=stop)

        def occupy_worker():
            occupied.set()
            release_worker.wait(5)

        async with app.run_test(size=(80, 24)) as pilot:
            await self.show_recent_details(pilot, app)
            with patch.object(tui_jobs.subprocess, "Popen") as launch, patch.object(
                    app, "notify", wraps=app.notify) as notify:
                try:
                    with patch.object(jobs, "_sha256", side_effect=pause_hash):
                        self.assertTrue(await pilot.click("#open-mp3"))
                        await self.wait_for(pilot, hashing.is_set)
                        waiting = loop.run_in_executor(None, occupy_worker)
                        release_hash.set()
                        await self.wait_for(pilot, occupied.is_set)
                        await self.wait_for(pilot, lambda: not app.session.inspections)
                    launch.assert_not_called()
                    audio.write_bytes(b"changed audio")
                finally:
                    release_hash.set()
                    release_worker.set()
                await waiting
                await self.wait_for(pilot, lambda: launch.called or notify.called)
                launch.assert_not_called()
                self.assertIn("Move it aside", notify.call_args.args[0])
            self.assertIn("Needs attention", self.screen_text(app))
            self.assertTrue(app.screen.query_one("#details-retry", Button).disabled)
            await pilot.press("escape")
            self.assertIn("Needs attention", self.screen_text(app))
            self.assertTrue(app.screen.query_one("#recent-retry", Button).disabled)

    async def test_confirmed_quit_cancels_open_waiting_for_dispatch(self):
        await self.quit_with_pending_open(after_checks=False)

    async def test_confirmed_quit_cancels_open_after_worker_checks(self):
        await self.quit_with_pending_open(after_checks=True)

    async def quit_with_pending_open(self, *, after_checks):
        self.save_job()
        active, pending = self.entry("Active"), self.entry("Pending")
        self.disk.save([active, pending])
        loop = asyncio.get_running_loop()
        loop.set_default_executor(ThreadPoolExecutor(max_workers=1))
        hashing, release_hash, occupied, release_worker = Event(), Event(), Event(), Event()
        folder_checked = Event()
        sha256 = jobs._sha256
        folder_problem = tui_jobs.download_folder_problem
        app = self.app()
        read_stop = app.session.quit_requested.is_set
        ui_thread = get_ident()

        def pause_hash(path, *, stop):
            hashing.set()
            if not release_hash.wait(5):
                raise TimeoutError("Receipt verification did not resume")
            return sha256(path, stop=stop)

        def occupy_worker():
            occupied.set()
            if not release_worker.wait(5):
                raise TimeoutError("Desktop dispatch did not resume")

        def check_folder(inspection):
            problem = folder_problem(inspection)
            folder_checked.set()
            return problem

        def check_stop():
            stopped = read_stop()
            if after_checks and folder_checked.is_set() and get_ident() != ui_thread:
                # Simulate Quit arriving just after the worker read a clear stop flag.
                occupy_worker()
            return stopped

        async def download(*args, **kwargs):
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                raise runner.AttemptStopped(planned=False) from None

        with patch.object(runner, "download", side_effect=download):
            async with app.run_test(size=(80, 24)) as pilot:
                await self.ready(pilot, app)
                self.assertTrue(await pilot.click("#primary"))
                await self.wait_for(pilot, lambda: app.session.active_id == active.id)
                await self.show_recent_details(pilot, app)
                with patch.object(jobs, "_sha256", side_effect=pause_hash), patch.object(
                        tui_jobs, "download_folder_problem", side_effect=check_folder), patch.object(
                        app.session.quit_requested, "is_set", side_effect=check_stop), patch.object(
                        tui_jobs.subprocess, "Popen") as launch:
                    waiting = None
                    try:
                        self.assertTrue(await pilot.click("#open-mp3"))
                        await self.wait_for(pilot, hashing.is_set)
                        if not after_checks:
                            waiting = loop.run_in_executor(None, occupy_worker)
                        release_hash.set()
                        await self.wait_for(pilot, occupied.is_set)
                        await self.wait_for(pilot, lambda: not app.session.inspections)
                        await pilot.press("escape", "escape", "ctrl+q")
                        self.assertIsInstance(app.screen, Confirm)
                        self.assertTrue(await pilot.click("#confirm"))
                        self.assertTrue(app.session.closing)
                    finally:
                        release_hash.set()
                        release_worker.set()
                    if waiting is not None:
                        await waiting
                    async with asyncio.timeout(5):
                        while app.return_code is None:
                            await asyncio.sleep(0.02)
                    launch.assert_not_called()
        self.assertEqual(app.return_code, 0)
        self.assertEqual([row.status for row in self.disk.read()], ["pending", "pending"])

    async def test_later_open_verification_updates_recent_job_after_first_result(self):
        _, audio, _ = self.save_job(keep_video=True)
        app = self.app()
        first, second = Event(), Event()
        first_release, second_release = Event(), Event()
        stages = iter(((first, first_release), (second, second_release)))
        inspect = jobs._inspect_outputs

        def pause_inspection(job, *, stop):
            started, release = next(stages)
            started.set()
            if not release.wait(5):
                raise TimeoutError("Open verification did not resume")
            return inspect(job, stop=stop)

        async with app.run_test(size=(80, 24)) as pilot:
            await self.show_recent_details(pilot, app)
            with patch.object(jobs, "_inspect_outputs", side_effect=pause_inspection), patch(
                    "ydl.tui_jobs.open_in_desktop") as launch, patch.object(app, "notify", wraps=app.notify) as notify:
                try:
                    self.assertTrue(await pilot.click("#open-mp3"))
                    await self.wait_for(pilot, first.is_set)
                    self.assertTrue(await pilot.click("#open-video"))
                    audio.unlink()
                    first_release.set()
                    await self.wait_for(pilot, second.is_set)
                    await self.wait_for(pilot, lambda: "Incomplete" in self.screen_text(app))
                    audio.write_bytes(b"changed audio")
                finally:
                    first_release.set()
                    second_release.set()
                await self.wait_for(pilot, lambda: notify.call_count == 2)
                launch.assert_not_called()
            self.assertIn("Needs attention", self.screen_text(app))
            self.assertTrue(app.screen.query_one("#details-retry", Button).disabled)
            await pilot.press("escape")
            self.assertIn("Needs attention", self.screen_text(app))
            self.assertIn("Move it aside", str(app.screen.query_one("#recent-note").content))
            self.assertTrue(app.screen.query_one("#recent-retry", Button).disabled)
