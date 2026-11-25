"""Exercise changed download targets through the Job Details Open actions."""

import hashlib
from threading import Event
from unittest.mock import patch

from textual.widgets import Button, DataTable

from ydl import jobs
from ydl.models import Settings

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
                await self.wait_for(pilot, lambda: launch.await_count or notify.called)
                launch.assert_not_awaited()
                self.assertIn("folder has changed", notify.call_args.args[0])
            self.assertIn("Incomplete", self.screen_text(app))
            self.assertFalse(app.screen.query_one("#details-retry", Button).disabled)

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
                await self.wait_for(pilot, lambda: launch.await_count or notify.called)
                launch.assert_not_awaited()
                self.assertIn(str(audio), notify.call_args.args[0])
            self.assertIn("Needs attention", self.screen_text(app))

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
                launch.assert_not_awaited()
            self.assertIn("Needs attention", self.screen_text(app))
            self.assertTrue(app.screen.query_one("#details-retry", Button).disabled)
            await pilot.press("escape")
            self.assertIn("Needs attention", self.screen_text(app))
            self.assertIn("Move it aside", str(app.screen.query_one("#recent-note").content))
            self.assertTrue(app.screen.query_one("#recent-retry", Button).disabled)
