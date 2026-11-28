"""Exercise queue removal through the offline runner and Textual Pilot."""

import json
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from ydl import config, jobs, media, runner
from ydl.models import Result, YdlError
from ydl.tui import YdlApp
from ydl.tui_dialogs import SettingsPanel
from ydl.tui_jobs import RowMenu
from ydl.tui_queue import decode_queue

from tui_pilot import PilotCase, URL
from tui_session_fixture import SessionCase


class QueueRemovalTests(SessionCase):
    async def test_verified_removal_keeps_partial_playlist_and_saved_outputs(self):
        playlist = replace(self.entry("Playlist", keep_video=True), remove_on_success=True)
        removable = replace(self.entry("Removable"), remove_on_success=True)
        kept = self.entry("Kept")
        plan = self.plan(playlist, "First", "Second", scope="playlist")
        transfer = self.offline_media()
        deliver = transfer.side_effect

        async def partial(command, video_id, *args, **kwargs):
            if video_id == "Second":
                raise YdlError("Track unavailable")
            return await deliver(command, video_id, *args, **kwargs)

        transfer.side_effect = partial
        session = await self.start(playlist, removable, kept)
        self.assertEqual(session.index.view(playlist.id).status, "Interrupted")
        with patch.object(media, "metadata", side_effect=[{"title": "Removable"}, {"title": "Kept"}]):
            await self.run_queue(session, "start")
        self.assertEqual([(row.id, row.status) for row in self.disk.read()],
                         [(playlist.id, "failed"), (kept.id, "complete")])
        self.assertEqual(session.index.view(playlist.id).inspection.verified, 1)
        self.assertTrue(self.store.load(removable.id).complete)

        transfer.side_effect = deliver
        app = YdlApp(self.paths, disk=self.disk)
        with patch("ydl.tui_jobs.open_in_desktop", return_value=None) as launch:
            async with app.run_test(size=(80, 24)) as pilot:
                await self.wait(lambda: app.session.index.runnable)
                self.assertEqual([row.id for row in app.session.index.rows], [playlist.id, kept.id])
                app.dashboard.query_one("#queue").focus()
                await pilot.press("d")
                details = app.screen
                self.assertTrue(await pilot.click("#details-retry"))
                await self.wait(lambda: not app.session.busy)
                self.assertEqual(self.disk.read(), [replace(kept, status="complete")])
                self.assertIs(app.screen, details)
                self.assertTrue(details.query_one("#open-folder").display)
                await pilot.press("escape")
                self.assertTrue(await pilot.click("#recent"))
                recent = app.screen
                await self.wait(lambda: playlist.id in recent.ids and app.session.external.get(playlist.id) is not None)
                table = recent.query_one("#recent-table")
                table.move_cursor(row=recent.ids.index(playlist.id))
                await pilot.press("enter")
                await pilot.pause()
                self.assertTrue(await pilot.click("#open-folder"))
                await self.wait(lambda: launch.await_count == 1)
                path, inspected, _ = launch.await_args.args
                self.assertEqual(path, Path(plan.destination))
                self.assertEqual(inspected.verified, 2)
                self.assertEqual({path.suffix for path in inspected.output_stamps}, {".mp3", ".mkv"})

    async def test_removal_requires_fresh_verification_and_a_successful_result(self):
        cases = {
            "unverified": Result(saved=1), "missingVideo": Result(saved=1), "declined": Result(declined=True),
            "failed": Result(failed=1), "attention": Result(attention="Needs attention"),
            "conflict": Result(output_conflict=True), "changed": Result(destination_changed=True),
        }
        rows = [replace(self.entry(name, keep_video=name == "missingVideo"), remove_on_success=True) for name in cases]
        session = await self.start(*rows)
        inspect = session.inspect

        async def download(_request, *_args, job_id, **_kwargs):
            row = session.index.row(job_id)
            name = row.url.split("=")[-1]
            if name not in ("unverified", "declined"):
                self.plan(row, name, complete=True)
            return cases[name]

        async def inspect_available(row_id, entry):
            if row_id != rows[0].id:
                return await inspect(row_id, entry)
            return None

        with patch.object(runner, "download", side_effect=download), patch.object(
                session, "inspect", side_effect=inspect_available):
            await self.run_queue(session, "start")
        self.assertEqual([row.id for row in self.disk.read()], [row.id for row in rows])
        self.assertEqual(session.index.view(rows[0].id).status, "Complete")
        self.assertEqual(session.index.view(rows[1].id).inspection.state, "unfinished")
        self.assertTrue(all(session.index.view(row.id).inspection.state == "complete" for row in rows[3:]))

    async def test_removal_save_failure_recovers_the_surviving_snapshot(self):
        transfer = self.offline_media()
        save, replace_path = self.disk.save, Path.replace
        for phase in ("before replacement", "during directory sync"):
            with self.subTest(phase=phase):
                name = phase.split()[0]
                row = replace(self.entry(name), remove_on_success=True)
                next_row = self.entry("Next")
                session = await self.start(row, next_row)

                def fail_replace(staged, target):
                    if target == self.disk.path:
                        raise OSError("queue replacement failed")
                    return replace_path(staged, target)

                def fail_removal(rows):
                    if rows != [next_row]:
                        return save(rows)
                    fault = (patch.object(Path, "replace", fail_replace) if phase == "before replacement"
                             else patch.object(config, "sync_directories", side_effect=OSError("queue sync failed")))
                    with fault:
                        save(rows)

                transfer.reset_mock()
                with patch.object(media, "metadata", return_value={"title": name}), patch.object(
                        self.disk, "save", side_effect=fail_removal):
                    await self.run_queue(session, "start")
                transfer.assert_awaited_once()
                self.assertFalse(session.armed)
                self.assertFalse(session.index.runnable)
                self.assertEqual(session.index.buffer[row.id].status, "complete")
                survived = phase == "before replacement"
                snapshot = decode_queue(self.disk.path.read_bytes())
                self.assertEqual(snapshot, [replace(row, status="active"), next_row] if survived else [next_row])
                self.assertTrue(await session.recover())
                self.assertEqual(self.disk.read(), [replace(row, status="complete"), next_row] if survived else [next_row])
                if survived:
                    self.assertEqual(session.index.view(row.id).status, "Complete")
                else:
                    self.assertIn(f"Job {row.id}: complete", session.index.notice)
                self.assertFalse(session.busy)
                self.assertEqual(session.index.buffer, {})


class RemovalOptionsTests(PilotCase):
    async def test_remove_on_success_label_fits_at_layout_breakpoints(self):
        self.disk.save([self.entry("Pending")])
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            for size in ((52, 24), (54, 24), (100, 26), (105, 26)):
                with self.subTest(size=size):
                    await pilot.resize_terminal(*size)
                    await pilot.pause()
                    self.assertIn("Remove from queue on success", self.screen_text(app))

    def test_remove_on_success_rejects_non_boolean_values(self):
        self.paths.config.write_text('remove_on_success = "false"\n')
        with self.assertRaisesRegex(YdlError, "remove_on_success must be true or false"):
            config.load(self.paths.config)
        entry = asdict(self.entry("Legacy"))
        entry["remove_on_success"] = 1
        with self.assertRaisesRegex(ValueError, "invalid queue options or state"):
            decode_queue(json.dumps({"schema_version": 1, "entries": [entry]}))

    async def test_remove_on_success_defaults_and_batch_override_at_compact_sizes(self):
        self.paths.config.write_text(f'output = "{self.output}"\n')
        legacy = asdict(self.entry("Legacy"))
        del legacy["remove_on_success"]
        self.disk.path.parent.mkdir(parents=True)
        self.disk.path.write_text(json.dumps({"schema_version": 1, "entries": [legacy]}))
        app = self.app(f"{URL}First\n{URL}Second")
        async with app.run_test(size=(80, 24)) as pilot:
            await self.ready(pilot, app)
            remove = app.dashboard.query_one("#remove-on-success")
            self.assertFalse(remove.value)
            self.assertIn("Keep video", next(line for line in self.rendered_lines(app) if "Remove from queue" in line))
            self.assertTrue(await pilot.click(remove))
            self.assertTrue(remove.value)
            app.dashboard.query_one("#retain").focus()
            await pilot.press("tab", "space")
            self.assertIs(app.focused, remove)
            self.assertFalse(remove.value)
            await pilot.press("space")
            self.assertTrue(remove.value)
            self.assertTrue(await pilot.click("#add-urls"))
            await self.wait_for(pilot, lambda: "Added 2 URLs" in self.screen_text(app))
            self.assertIs(app.screen, app.dashboard)
            self.assertEqual([row.remove_on_success for row in self.disk.read()], [False, True, True])

            self.assertTrue(await pilot.click("#settings"))
            await self.wait_for(pilot, lambda: isinstance(app.screen, SettingsPanel) and app.screen.ready)
            setting = app.screen.query_one("#settings-remove-on-success")
            self.assertFalse(setting.value)
            app.screen.query_one("#settings-video").focus()
            await pilot.press("tab", "space")
            self.assertIs(app.focused, setting)
            self.assertTrue(setting.value)
            self.assertTrue(await pilot.click("#save-settings"))
            await self.wait_for(pilot, lambda: app.screen is app.dashboard)
            self.assertTrue(config.load(self.paths.config).remove_on_success)
            self.assertTrue(remove.value)
            self.assertEqual([row.remove_on_success for row in self.disk.read()], [False, True, True])

            await pilot.resize_terminal(100, 26)
            await pilot.pause()
            self.assertTrue(await pilot.click(remove))
            self.assertFalse(remove.value)
            queue = app.dashboard.query_one("#queue")
            action_x = sum(column.width + 2 * queue.cell_padding for column in queue.ordered_columns[:-1]) + 1
            self.assertTrue(await pilot.click(queue, offset=(action_x, 2)))
            self.assertTrue(await pilot.click("#details"))
            self.assertTrue(await pilot.click("#--content-tab-info-pane"))
            self.assertIn("Remove from queue on success  Yes", self.screen_text(app))
            await pilot.press("escape")
            await pilot.resize_terminal(80, 24)
            await pilot.press("f2")
            self.assertIsInstance(app.screen, RowMenu)
