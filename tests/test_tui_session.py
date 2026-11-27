"""Exercise queue handoff, backend readiness and plan reconciliation without YouTube."""

import asyncio
import os
import sys
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event as ThreadEvent
from unittest.mock import patch

from ydl import config, jobs, media, process, runner
from ydl.models import Result, YdlError
from ydl.tui_queue import Entry

from tui_session_fixture import SessionCase


class SessionTests(SessionCase):
    async def test_obsolete_backend_failure_waits_for_new_preferences(self):
        row = self.entry("A")
        checking = asyncio.Event()
        finish_nightly = asyncio.Event()
        channels = []

        async def ensure(_path, prefs, **_kwargs):
            channels.append(prefs.channel)
            if prefs.channel == "nightly":
                checking.set()
                await finish_nightly.wait()
                raise YdlError("Nightly temporary failure")

        self.backend.side_effect = ensure
        session = await self.start(row)
        await checking.wait()
        with patch.object(runner, "download", return_value=Result(saved=1)) as download:
            session.arm("start")
            await session.backend.save(asdict(replace(self.prefs, channel="stable", auto_update=True)))
            self.assertEqual(self.disk.read(), [row])
            download.assert_not_called()
            finish_nightly.set()
            await session.serial
            download.assert_awaited_once()
        self.assertEqual(channels, ["nightly", "stable"])
        self.assertEqual(self.disk.read()[0].status, "complete")

    async def test_stop_disarms_backend_and_hard_failure_keeps_rows(self):
        row = self.entry("A")
        checking = asyncio.Event()
        cancelled = asyncio.Event()

        async def ensure(*_args, **_kwargs):
            checking.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        self.backend.side_effect = ensure
        session = await self.start(row)
        await checking.wait()
        with patch.object(runner, "download") as download:
            session.arm("start")
            await session.stop()
            self.assertTrue(cancelled.is_set())
            self.assertEqual(self.disk.read(), [row])
            self.backend.side_effect = YdlError("Failed without usable yt-dlp")
            await self.run_queue(session, "start")
            self.assertEqual(session.backend.status.state, "Unavailable")
            self.assertEqual(self.disk.read(), [row])
            download.assert_not_called()

    async def test_dispatch_rechecks_removed_backend_before_active(self):
        row = self.entry("A")
        session = await self.start(row)
        await self.wait(lambda: session.backend.check is None)
        self.paths.backend.unlink()
        self.version.return_value = None

        async def missing(*_args, **_kwargs):
            self.assertEqual(self.disk.read(), [row])
            raise YdlError("Could not install backend")

        self.backend.side_effect = missing
        with patch.object(runner, "download") as download:
            await self.run_queue(session, "start")
            download.assert_not_called()
        self.assertEqual(self.backend.await_count, 2)
        self.assertEqual(self.disk.read(), [row])

    async def test_dispatch_rechecks_backend_removed_while_waiting_for_queue_edit(self):
        row = self.entry("A")
        executable = self.paths.backend
        session = await self.start(row)
        await self.wait(lambda: session.backend.check is None)

        async def version(*_args):
            return "2026.01.01" if executable.exists() else None

        self.version.side_effect = version
        self.backend.side_effect = YdlError("Could not install backend")
        with patch.object(self.disk, "save", wraps=self.disk.save) as save, \
                patch.object(runner, "download", return_value=Result(saved=1)) as download:
            serial = await self.change_at_dispatch(session, executable.unlink)
            await asyncio.wait_for(serial, 4)
            download.assert_not_called()
            save.assert_not_called()
        self.assertEqual(self.backend.await_count, 2)
        self.assertEqual(session.backend.status.state, "Unavailable")
        self.assertEqual(self.disk.read(), [row])

    async def test_invalid_preferences_stop_selection_after_settlement(self):
        first, second = [self.entry(name) for name in "AB"]
        session = await self.start(first, second)

        async def first_attempt(*_args, **_kwargs):
            self.paths.config.write_text("[[invalid")
            return Result(saved=1)

        with patch.object(runner, "download", side_effect=first_attempt) as download:
            await self.run_queue(session, "start")
            download.assert_awaited_once()
            self.assertIn(str(self.paths.config), session.backend.config_error)
            self.assertEqual([row.status for row in self.disk.read()], ["complete", "pending"])
            config.save(self.paths.config, asdict(self.prefs))
            await session.retry_backend()
            await self.wait(lambda: session.backend.check is None)
            self.assertFalse(session.backend.config_error)
            self.assertFalse(session.busy)

    async def test_invalid_preferences_at_dispatch_leave_pending_untouched(self):
        row = self.entry("A")
        session = await self.start(row)
        await self.wait(lambda: session.backend.check is None)

        with patch.object(self.disk, "save") as save, patch.object(runner, "download") as download:
            serial = await self.change_at_dispatch(session, lambda: self.paths.config.write_text("[[invalid"))
            await asyncio.wait_for(serial, 4)
            save.assert_not_called()
            download.assert_not_called()
        self.assertFalse(session.armed)
        self.assertEqual(self.disk.read(), [row])
        self.assertIn(str(self.paths.config), session.backend.config_error)
        with self.assertRaises(YdlError) as invalid:
            config.load(self.paths.config)
        self.assertIn(str(invalid.exception), session.backend.config_error)

    async def test_changed_channel_at_dispatch_waits_for_new_readiness(self):
        row = self.entry("A")
        session = await self.start(row)
        await self.wait(lambda: session.backend.check is None)
        ready = asyncio.Event()
        changed = replace(self.prefs, channel="stable")

        async def ensure(_path, prefs, **_kwargs):
            self.assertEqual(prefs, changed)
            self.assertFalse(session.index.lock.locked())
            await ready.wait()

        self.backend.side_effect = ensure
        with patch.object(self.disk, "save", wraps=self.disk.save) as save:
            with patch.object(runner, "download", return_value=Result(saved=1)) as download:
                serial = await self.change_at_dispatch(
                    session, lambda: config.save(self.paths.config, asdict(changed)))
                try:
                    await self.wait(lambda: self.backend.await_count == 2 or not session.busy)
                    self.assertEqual(self.backend.await_count, 2)
                    self.assertIsNotNone(session.backend.check)
                    save.assert_not_called()
                    download.assert_not_called()
                    self.assertEqual(self.disk.read(), [row])
                finally:
                    ready.set()
                await asyncio.wait_for(serial, 4)
                download.assert_awaited_once()
                self.assertEqual(download.await_args.args[2], changed)
        self.assertEqual(self.backend.await_count, 2)
        self.assertEqual([call.args[0][0].status for call in save.call_args_list], ["active", "complete"])

    async def change_at_dispatch(self, session, change):
        waiting = asyncio.Event()
        lock = session.index.lock
        acquire = lock.acquire

        async def at_dispatch():
            waiting.set()
            return await acquire()

        async with lock:
            with patch.object(lock, "acquire", at_dispatch):
                session.arm("start")
                serial = session.serial
                await asyncio.wait_for(waiting.wait(), 4)
                change()
        return serial

    async def test_stop_waits_for_timed_out_command_cleanup(self):
        session = await self.start(self.entry("A"), self.entry("B"))
        ready, cleaning, release, exited = (self.root / name for name in ("ready", "cleaning", "release", "exited"))
        command = """
import signal, sys, time
from pathlib import Path

ready, cleaning, release, exited = map(Path, sys.argv[1:])

def stop(*_):
    cleaning.touch()
    while not release.exists():
        time.sleep(0.01)
    exited.touch()
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
ready.touch()
while True:
    time.sleep(0.01)
"""
        deadline = asyncio.timeout(None)

        async def download(*_args, **_kwargs):
            async with deadline:
                await process.run([sys.executable, "-c", command, *map(str, (ready, cleaning, release, exited))])

        with patch.object(runner, "download", side_effect=download) as attempt:
            session.arm("start")
            stop = None
            try:
                await self.wait(ready.exists)
                deadline.reschedule(asyncio.get_running_loop().time())
                await self.wait(cleaning.exists)
                stop = asyncio.create_task(session.stop())
                await asyncio.wait({stop}, timeout=0.2)
                self.assertTrue(session.stopping)
                session.arm("start")
                attempt.assert_awaited_once()
            finally:
                release.touch()
                if stop is not None:
                    await asyncio.wait_for(stop, 4)
                if cleaning.exists():
                    await self.wait(exited.exists)
            self.assertEqual([row.status for row in self.disk.read()], ["pending", "pending"])

    async def test_stop_refreshes_verified_outputs_of_a_resumed_playlist(self):
        row = replace(self.entry("A", status="interrupted"), remove_on_success=True)
        plan = self.plan(row, "A", "B", scope="playlist")
        session = await self.start(row)
        published = asyncio.Event()

        async def retry(*args, **kwargs):
            with self.store.claim(row.id):
                self.save_mp3(plan, plan.items[0])
                self.store.save(plan)
                published.set()
                await asyncio.Future()

        with patch.object(runner, "retry", side_effect=retry):
            session.arm("start")
            async with asyncio.timeout(4):
                await published.wait()
                await session.stop()
        view = session.index.view(row.id)
        self.assertEqual(view.status, "Interrupted")
        self.assertEqual(view.inspection.verified, 1)
        self.assertEqual(self.disk.read()[0].status, "interrupted")

    async def test_verification_holds_lock_and_gates_failed_snapshot(self):
        first, second = [self.entry(name, status="failed") for name in "AB"]
        first_plan = self.plan(first, "A", "B", scope="playlist")
        mp3 = self.save_mp3(first_plan, first_plan.items[0])
        self.store.save(first_plan)
        self.plan(second, "B")
        checking = asyncio.Event()
        release = ThreadEvent()
        loop = asyncio.get_running_loop()
        sha256 = jobs._sha256

        def paused_verification(path, *, stop):
            if path == mp3:
                loop.call_soon_threadsafe(checking.set)
                if not release.wait(4):
                    raise TimeoutError("Receipt verification was not released")
            return sha256(path, stop=stop)

        self.disk.save([first, second])
        session = self.make_session()
        with patch.object(jobs, "_sha256", side_effect=paused_verification):
            opening = asyncio.create_task(session.open())
            try:
                async with asyncio.timeout(4):
                    await checking.wait()
                await self.wait(lambda: session.index.view(second.id).status == "Failed")
                self.assertEqual(session.index.view(first.id).status, "Verifying")
                session.arm("failed")
                self.assertFalse(session.busy)
                code, error = await self.cli_retry(first.id)
                self.assertEqual(code, 1)
                self.assertIn("Job already running", error)
            finally:
                release.set()
                await opening
        with patch.object(runner, "retry", return_value=Result(failed=1)) as retry:
            await self.run_queue(session, "failed")
            self.assertEqual([call.args[0] for call in retry.call_args_list], [first.id, second.id])
        self.save_mp3(first_plan, first_plan.items[1])
        self.store.save(first_plan)
        self.assertEqual((await self.cli_retry(first.id))[0], 0)
        await session.index.recheck(first.id)
        self.assertEqual(session.index.view(first.id).status, "Complete")
        self.assertEqual(self.disk.read()[0].status, "complete")

    async def cli_retry(self, job_id):
        env = {**os.environ, "XDG_STATE_HOME": str(self.root / "state"), "XDG_CONFIG_HOME": str(self.root / "config"),
               "XDG_DATA_HOME": str(self.root / "data")}
        process = await asyncio.create_subprocess_exec(sys.executable, "-m", "ydl", "retry", job_id, env=env,
                                                       stderr=asyncio.subprocess.PIPE)
        _, error = await process.communicate()
        return process.returncode, error.decode()

    async def test_missing_plan_preserves_failed_category_and_explicit_add_remedy(self):
        row = self.entry("A", status="failed")
        self.plan(row, "A")
        self.offline_media().side_effect = YdlError("Track unavailable")
        path = self.store.path(row.id)
        backup = self.root / "original-plan.json"
        session = await self.start(row)
        core_retry = runner.retry
        inspect_job = jobs.inspect_job

        async def remove_before_retry(*args, **kwargs):
            path.rename(backup)
            return await core_retry(*args, **kwargs)

        async def remove_before_verification(*args, **kwargs):
            path.rename(backup)
            return await inspect_job(*args, **kwargs)

        for boundary, remove_plan in (
            ("dispatch", patch.object(runner, "retry", side_effect=remove_before_retry)),
            ("settlement", patch.object(jobs, "inspect_job", side_effect=remove_before_verification)),
        ):
            with self.subTest(boundary=boundary):
                with remove_plan, patch.object(runner, "download") as download:
                    await self.run_queue(session, "failed")
                    download.assert_not_called()
                saved = self.disk.read()[0]
                self.assertEqual(saved.status, "failed")
                if boundary == "settlement":
                    self.assertIn("Track unavailable", saved.diagnostic)
                view = session.index.view(row.id)
                self.assertEqual(view.status, "Needs attention")
                self.assertIn(str(path), view.diagnostic)
                self.assertIn("Add to start fresh", view.diagnostic)
                session.arm("failed")
                self.assertFalse(session.busy)
                backup.rename(path)
                await session.index.recheck(row.id)
                self.assertEqual(session.index.view(row.id).status, "Failed")
                self.assertTrue(session.index.view(row.id).planned)

    async def test_fully_verified_plan_without_completion_flag_and_changed_output(self):
        row = self.entry("A", status="active")
        plan = self.plan(row, "A", complete=True)
        session = await self.start(row)
        self.assertEqual(session.index.view(row.id).status, "Complete")
        path = Path(plan.destination) / plan.items[0].outputs["mp3"]["name"]
        path.write_bytes(b"a different file")
        await session.index.recheck(row.id)
        self.assertEqual(session.index.view(row.id).status, "Needs attention")
        self.assertEqual(self.disk.read()[0].status, "complete")
        self.assertEqual(path.read_bytes(), b"a different file")

    async def test_active_save_stop_restores_prior_hint_and_corrupt_index_is_preserved(self):
        row = self.entry("A")
        session = await self.start(row)
        save = self.disk.save
        loop = asyncio.get_running_loop()
        reached = asyncio.Event()

        def checkpoint(rows):
            save(rows)
            if rows[0].status == "active":
                loop.call_soon_threadsafe(disarm)

        def disarm():
            session.armed = False
            reached.set()

        with patch.object(self.disk, "save", side_effect=checkpoint), patch.object(runner, "download") as download:
            await self.run_queue(session, "start")
            self.assertTrue(reached.is_set())
            download.assert_not_called()
        self.assertEqual(self.disk.read(), [row])
        original = b'{"schema_version":7,"entries":[]}'
        self.disk.path.write_bytes(original)
        reopened = self.make_session()
        await reopened.open()
        self.assertIn(str(self.disk.path), reopened.index.error)
        self.assertFalse(await reopened.recover())
        self.assertEqual(self.disk.path.read_bytes(), original)

    async def test_reserved_plan_uncertain_save_needs_recheck_before_retry(self):
        row = self.entry("A")
        session = await self.start(row)
        sync = config.sync_directories

        def fail_jobs_sync(path):
            if path == self.store.directory:
                raise OSError("jobs directory fsync failed")
            sync(path)

        deps = runner.Dependencies([], "ffmpeg", "ffprobe")
        with patch.object(runner, "prepare", return_value=deps), patch.object(media, "metadata", return_value={"title": "A"}):
            with patch.object(config, "sync_directories", side_effect=fail_jobs_sync), patch.object(media, "transfer") as transfer:
                await self.run_queue(session, "start")
                transfer.assert_not_called()
        self.assertTrue(self.store.path(row.id).is_file())
        self.assertEqual(self.disk.read()[0].status, "interrupted")
        self.assertEqual(session.index.view(row.id).status, "Needs attention")
        await session.index.recheck(row.id)
        self.assertEqual(session.index.view(row.id).status, "Interrupted")
        with patch.object(runner, "retry", return_value=Result(saved=1)) as retry:
            await self.run_queue(session, "start")
            self.assertEqual(retry.call_args.args[0], row.id)

    async def test_changed_availability_keeps_saved_order_and_advances_to_next_url(self):
        new, saved, last = [self.entry(name) for name in ("removed", "oldPlaylist", "last")]
        plan = self.plan(saved, "verifiedGone", "gone", "stillHere", scope="playlist")
        completed = self.save_mp3(plan, plan.items[0])
        identity = completed.stat().st_ino
        self.store.save(plan)
        transfer = self.offline_media()
        deliver = transfer.side_effect

        async def metadata(_cmd, url, **_kwargs):
            if "removed" in url:
                raise YdlError("This video or playlist is private, removed, or unavailable.")
            self.assertIn("last", url)
            return {"title": "Last video"}

        async def available(command, url, *args):
            if url.endswith("=gone"):
                raise YdlError("Video unavailable")
            return await deliver(command, url, *args)

        transfer.side_effect = available
        session = await self.start(new, saved, last)
        with patch.object(media, "metadata", side_effect=metadata) as lookup:
            await self.run_queue(session, "start")
            self.assertEqual(lookup.await_count, 2)
        self.assertEqual([call.args[1].split("=")[-1] for call in transfer.call_args_list], ["gone", "stillHere", "last"])
        self.assertEqual(completed.stat().st_ino, identity)
        self.assertEqual([row.status for row in self.disk.read()], ["failed_before_plan", "failed", "complete"])
        self.assertEqual([row.id for row in self.disk.read()], [new.id, saved.id, last.id])
        self.assertEqual([item.video_id for item in self.store.load(saved.id).items], ["verifiedGone", "gone", "stillHere"])

    async def test_destination_redirect_and_replacement_prevent_publication(self):
        row = self.entry("A")
        elsewhere = self.root / "elsewhere"
        self.output.rename(elsewhere)
        self.output.symlink_to(elsewhere, target_is_directory=True)
        session = await self.start(row)
        self.assertEqual(session.index.view(row.id).status, "Needs attention")
        self.assertFalse(session.candidates("start"))
        self.output.unlink()
        elsewhere.rename(self.output)
        await session.index.recheck(row.id)
        self.assertEqual(session.index.view(row.id).status, "Pending")
        transfer = self.offline_media()

        async def verify(_tool, _path, _kind, _log):
            self.output.rename(elsewhere)
            self.output.mkdir()

        with patch.object(media, "metadata", return_value={"title": "Media title"}):
            with patch.object(media, "verify_output", side_effect=verify):
                await self.run_queue(session, "start")
        transfer.assert_awaited_once()
        self.assertEqual(self.disk.read()[0].status, "interrupted")
        view = session.index.view(row.id)
        self.assertEqual(view.status, "Needs attention")
        self.assertIn(str(self.output), view.diagnostic)
        self.assertEqual(list(self.output.glob("*.mp3")), [])
        self.assertIsNotNone(view.inspection)
        self.assertEqual(view.inspection.job, self.store.load(row.id))
        self.output.rmdir()
        elsewhere.rename(self.output)
        await session.index.recheck(row.id)
        self.assertEqual(session.index.view(row.id).status, "Interrupted")

    async def test_new_playlist_cannot_create_a_folder_through_a_replaced_destination(self):
        row = Entry.from_url("https://www.youtube.com/playlist?list=List", "playlist", "0", False, self.output)
        session = await self.start(row)
        transfer = self.offline_media()
        redirected = self.root / "redirected"
        redirected.mkdir()
        original = self.root / "original"
        real_mkdir = Path.mkdir

        def replace_destination(path, *args, **kwargs):
            result = real_mkdir(path, *args, **kwargs)
            if path == self.output and not original.exists():
                path.rename(original)
                path.symlink_to(redirected)
            return result

        metadata = {"title": "Playlist title", "entries": [{"id": "Track", "title": "Track"}]}
        with patch.object(media, "metadata", return_value=metadata), patch.object(Path, "mkdir", replace_destination):
            await self.run_queue(session, "start")

        transfer.assert_not_awaited()
        self.assertEqual(session.index.view(row.id).status, "Needs attention")
        self.assertFalse(self.store.path(row.id).exists())
        self.assertTrue(self.output.is_symlink())
        self.assertEqual(list(redirected.iterdir()), [])

    async def test_destination_change_after_preplan_retry_keeps_plan_required(self):
        row = self.entry("A", status="failed_before_plan")
        session = await self.start(row)
        self.offline_media()

        async def replace_destination(*_):
            self.output.rename(self.root / "original-output")
            self.output.mkdir()

        with patch.object(media, "metadata", return_value={"title": "Media title"}), patch.object(
                media, "verify_output", side_effect=replace_destination):
            await self.run_queue(session, "failed")
        self.assertEqual(session.index.view(row.id).status, "Needs attention")
        self.store.path(row.id).rename(self.root / "original-plan.json")
        await session.stop(quitting=True)

        reopened = self.make_session()
        await reopened.open()
        view = reopened.index.view(row.id)
        self.assertEqual(view.status, "Needs attention")
        self.assertIn("Add to start fresh", view.diagnostic)
        self.assertFalse(reopened.candidates("failed"))
        self.assertEqual(self.disk.read()[0].status, "failed")

    async def test_first_video_conflict_records_target_and_keeps_processing(self):
        claimed, last = self.entry("List", keep_video=True), self.entry("last")
        plan = self.plan(claimed, "blocked", "nextTrack", scope="playlist")
        target = Path(plan.destination) / f"{plan.items[0].stem}.mkv"
        session = await self.start(claimed, last)
        transfer = self.offline_media()
        deliver = transfer.side_effect

        async def occupy(command, url, *args):
            source = await deliver(command, url, *args)
            if url.endswith("=blocked"):
                target.write_bytes(b"Existing file")
            return source

        transfer.side_effect = occupy
        with patch.object(media, "metadata", return_value={"title": "Last video"}):
            await self.run_queue(session, "start")
        self.assertEqual([call.args[1].split("=")[-1] for call in transfer.call_args_list], ["blocked", "nextTrack", "last"])
        self.assertEqual(target.read_bytes(), b"Existing file")
        item = self.store.load(claimed.id).items[0]
        self.assertEqual(item.video_target, target.name)
        self.assertNotIn("video", item.outputs)
        self.assertEqual(session.index.view(claimed.id).status, "Needs attention")
        self.assertEqual(self.disk.read()[0].status, "failed")
        target.rename(target.with_suffix(".original"))
        await session.index.recheck(claimed.id)
        self.assertEqual(session.index.view(claimed.id).status, "Failed")

    async def test_preplan_failure_retries_original_id_and_options(self):
        row = replace(self.entry("A", keep_video=True), quality="256K", scope="single")
        session = await self.start(row)
        failure = YdlError("YouTube did not provide a usable requested format.")
        with patch.object(runner, "download", side_effect=[failure, Result(saved=1)]) as download:
            await self.run_queue(session, "start")
            self.assertEqual(self.disk.read()[0].status, "failed_before_plan")
            self.assertIn(str(failure), self.disk.read()[0].diagnostic)
            session.arm("selected", row.id)
            await session.serial
        self.assertEqual([call.kwargs["job_id"] for call in download.await_args_list], [row.id, row.id])
        settings = download.await_args.args[0]
        self.assertEqual((settings.scope, settings.quality, settings.keep_video, settings.output),
                         ("single", "256K", True, row.output))

    async def test_pending_edits_committed_during_backend_wait_control_dispatch_order(self):
        first, second, third = [self.entry(name) for name in "ABC"]
        release = asyncio.Event()

        async def ensure(*_args, **_kwargs):
            await release.wait()

        self.backend.side_effect = ensure
        session = await self.start(first, second, third)
        with patch.object(runner, "download", return_value=Result(saved=1)) as download:
            session.arm("start")
            try:
                self.assertTrue(await session.index.edit(lambda rows: rows[1:]))
                self.assertTrue(await session.index.edit(lambda rows: rows[::-1]))
                self.assertEqual(self.disk.read(), [third, second])
            finally:
                release.set()
            await session.serial
        self.assertEqual([call.kwargs["job_id"] for call in download.await_args_list], [third.id, second.id])

    async def test_stop_with_failed_interruption_save_buffers_pending_and_pauses_launch(self):
        first, later = [self.entry(name) for name in "AB"]
        session = await self.start(first, later)
        downloading = asyncio.Event()
        save = self.disk.save

        def fail_interrupt(rows):
            if rows[0].status == "pending":
                raise OSError("Cannot save interrupted attempt")
            save(rows)

        async def wait_for_stop(*_args, **_kwargs):
            downloading.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                raise runner.AttemptStopped(planned=False) from None

        with patch.object(runner, "download", side_effect=wait_for_stop) as download:
            with patch.object(self.disk, "save", side_effect=fail_interrupt):
                session.arm("start")
                await downloading.wait()
                await session.stop()
            download.assert_awaited_once()
        self.assertIn("Cannot save interrupted attempt", session.index.error)
        self.assertEqual(session.index.buffer[first.id].status, "pending")
        self.assertTrue(await session.recover())
        self.assertEqual([row.status for row in self.disk.read()], ["pending", "pending"])
        self.assertFalse(session.busy)
