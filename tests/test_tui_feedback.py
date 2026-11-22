"""Exercise queue controls and diagnostics while the dashboard is on screen."""

import asyncio
from dataclasses import replace
from unittest.mock import patch

from ydl import jobs, runner
from ydl.models import Result, YdlError
from ydl.tui import YdlApp
from ydl.tui_dialogs import Confirm
from ydl.tui_queue import QueueFile

from tui_pilot import PilotCase


class FeedbackTests(PilotCase):
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
                    await self.submit(pilot)
                    self.assertIsInstance(app.screen, Confirm)
                    await pilot.click("#cancel")
                    await self.wait_for(pilot, lambda: not app.screen.saving)
                    self.assertEqual(self.disk.read(), [row])
                finally:
                    resume.set()
                    await self.ready(pilot, app)

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
            self.assertIn("Queue restored", self.screen_text(app))
            finish.set()
            await self.wait_for(pilot, lambda: app.session.backend.check is None)
            await pilot.pause()
            text = self.screen_text(app)
            self.assertIn("Release service returned HTTP 503", text)
            self.assertNotIn("Queue restored", text)

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

    @staticmethod
    def screen_text(app):
        return "\n".join(strip.text for strip in app.screen._compositor.render_strips())
