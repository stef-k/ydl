"""Own the queue's one serial action and settle each invoked operation."""

import asyncio
from dataclasses import replace
from pathlib import Path

from . import jobs, runner
from .models import Event, YdlError
from .tui_queue import STARTABLE, QueueIndex, View, classify, missing_plan_message
from .tui_backend import BackendReadiness, backend_key


class QueueSession:
    def __init__(self, paths, disk, changed, decide):
        self.paths, self.changed, self.decide = paths, changed, decide
        self.store = jobs.JobStore(paths.state)
        self.index = QueueIndex(disk, self.store, self.index_changed, self.inspect)
        self.backend = BackendReadiness(paths, self.backend_changed)
        self.inspections = set()
        self.serial = None
        self.operation = None
        self.armed = False
        self.stopping = False
        self.closing = False
        self.active_id = None
        self.events = {}
        self.plans = {}
        self.external = {}
        self.external_results = {}
        self.external_retry_id = None
        self.notice = ""

    def index_changed(self):
        if self.index.error:
            self.armed = False
        self.changed()

    def backend_changed(self, *, defaults=False):
        if self.backend.config_error:
            self.armed = False
        if self.backend.config_error or self.backend.status.detail:
            self.index.dismiss_restore_notice()
        self.changed(defaults=defaults)

    async def open(self):
        await self.backend.read(launch=True)
        self.changed(defaults=True)
        await self.index.open(self.startup_backend)

    def startup_backend(self):
        if self.backend.startup_check_owed and not (self.backend.config_error or self.closing):
            self.backend.start_check()

    @property
    def busy(self):
        return self.serial is not None or self.stopping

    @property
    def media_allowed(self):
        return self.index.runnable and not (self.busy or self.closing or self.backend.repair_at_launch)

    @property
    def external_allowed(self):
        return self.index.editable and not (self.busy or self.closing or self.backend.repair_at_launch)

    @property
    def can_recover(self):
        return bool(self.index.error and not (self.busy or self.closing or self.index.verifying or
                                             self.index.recovering or self.backend.check))

    @property
    def backend_retry_visible(self):
        return self.backend.config_error or self.backend.status.state in ("Unavailable", "Check pending")

    @property
    def can_retry_backend(self):
        return (self.backend_retry_visible and self.index.editable and
                not (self.busy or self.backend.check or self.closing))

    async def recover(self):
        if not self.can_recover:
            return False
        if not await self.index.recover():
            return False
        self.startup_backend()
        return True

    async def retry_backend(self):
        if not self.can_retry_backend:
            return
        if await self.backend.read() and self.can_retry_backend:
            self.index.dismiss_restore_notice()
            self.backend.start_check()

    def selected_retry(self, row_id):
        view = self.index.view(row_id)
        return view.status == "Failed" or (view.status == "Interrupted" and view.planned)

    def candidates(self, mode, target=None):
        rows = self.index.rows
        if mode == "selected":
            return [row.id for row in rows if row.id == target and self.selected_retry(row.id)]
        eligible = STARTABLE if mode == "start" else {"Failed"}
        return [row.id for row in rows if self.index.view(row.id).status in eligible]

    def arm(self, mode, row_id=None):
        if mode == "external":
            target = self.external.get(row_id)
            targets = [row_id] if target and target.state == "unfinished" else []
        else:
            targets = self.candidates(mode, row_id)
        allowed = self.external_allowed if mode == "external" else self.media_allowed
        if not allowed or not targets:
            return
        self.armed = True
        self.external_retry_id = row_id if mode == "external" else None
        self.notice = ""
        self.index.clear_notice()
        self.serial = asyncio.create_task(self.run_action(mode, targets))
        self.changed()

    def allowed(self, mode):
        available = self.index.editable if mode == "external" else self.index.runnable
        return self.armed and available and not self.closing

    def next_id(self, mode, targets, attempted):
        rows = targets if mode == "external" else self.candidates(mode, targets[0])
        return next((row_id for row_id in rows if row_id not in attempted and
                     (mode == "start" or row_id in targets)), None)

    async def run_action(self, mode, targets):
        attempted = set()
        allowed = lambda: self.allowed(mode)
        try:
            while allowed() and self.next_id(mode, targets, attempted):
                self.operation = asyncio.create_task(self.backend.prepare(allowed))
                try:
                    prepared = await self.operation
                except asyncio.CancelledError:
                    break
                finally:
                    self.operation = None
                if prepared is None or not allowed():
                    break
                prefs, stamp = prepared
                if mode == "external":
                    if stamp != self.backend.executable_stamp():
                        continue
                    await self.run_external(targets[0], prefs)
                    break
                async with self.index.lock:
                    current = await self.backend.read() if allowed() else None
                    if current is None or not allowed():
                        break
                    # Release checks must run outside the queue mutation lock.
                    key = backend_key(current)
                    if key != backend_key(prefs) or key != self.backend.ready_key:
                        continue
                    if stamp != self.backend.executable_stamp():
                        continue
                    entry = self.index.row(self.next_id(mode, targets, attempted))
                    view = await self.index.activate(entry) if entry is not None else None
                if view is None:
                    break
                attempted.add(entry.id)
                await self.run_entry(mode, entry, view, current)
        finally:
            self.armed = False
            self.external_retry_id = None
            self.serial = None
            self.changed()

    def record(self, row_id, event):
        if event.settings is not None:
            self.plans[row_id] = event
        if event.stage not in ("job", "settings"):
            self.events[row_id] = event
        self.changed(progress=True)

    async def invoke(self, mode, row_id, prefs, *, entry=None, planned=True):
        # Stop can run after this task is queued and before it enters the runner.
        if not self.allowed(mode):
            return None
        self.active_id = row_id
        self.events[row_id] = Event("checking", "Preparing download")
        self.external_results.pop(row_id, None)
        self.changed()
        args = dict(emit=lambda event: self.record(row_id, event), decide=self.decide, backend_ready=True)
        if planned:
            return await runner.retry(row_id, self.paths, prefs, **args)
        scope = None if entry.scope == "auto" else entry.scope
        request = runner.Download(entry.url, scope, entry.quality, entry.keep_video, entry.output)
        return await runner.download(request, self.paths, prefs, job_id=row_id, **args)

    async def run_entry(self, mode, entry, view, prefs):
        settled, display = entry, view
        self.operation = asyncio.create_task(self.invoke(mode, entry.id, prefs, entry=entry, planned=view.planned))
        try:
            result = await self.operation
        except (OSError, TimeoutError, YdlError, asyncio.CancelledError) as exc:
            if self.active_id == entry.id:
                settled, display = await self.entry_error(entry, view, exc)
        else:
            if result is not None:
                settled, display = await self.entry_result(entry, result)
                display = replace(display, result=result)
        finally:
            self.operation = None
        await self.index.settle(settled, display)
        self.plans.pop(entry.id, None)
        self.active_id = None
        self.changed()

    async def entry_error(self, entry, view, exc):
        diagnostic = str(exc) or "Download stopped."
        if isinstance(exc, jobs.OccupiedPlan):
            observed = await self.inspect(entry.id, entry)
            if observed is None:
                return replace(entry, diagnostic=diagnostic), view
            hint, display = classify(entry, observed, self.store)
            return replace(entry, status=hint, diagnostic=diagnostic), display
        if isinstance(exc, jobs.MissingPlan):
            diagnostic = missing_plan_message(self.store, entry.id)
        if isinstance(exc, runner.UncertainPlan):
            return replace(entry, status="interrupted", diagnostic=diagnostic), View("Needs attention", diagnostic=diagnostic)
        if isinstance(exc, (jobs.MissingPlan, jobs.JobBusy, jobs.PlanNeedsSync, jobs.DestinationChanged)):
            return replace(entry, diagnostic=diagnostic), View("Needs attention", view.inspection, diagnostic)
        return await self.attempt_outcome(entry, view, exc, diagnostic)

    async def attempt_outcome(self, entry, view, exc, diagnostic):
        planned = getattr(exc, "planned", view.planned or self.store.path(entry.id).exists())
        if isinstance(exc, asyncio.CancelledError):
            status = "interrupted" if planned else "pending"
        elif isinstance(exc, runner.AttemptDismissed):
            status = "interrupted" if planned else "cancelled"
        else:
            status = "failed" if planned else "failed_before_plan"
        labels = {"interrupted": "Interrupted", "pending": "Pending", "cancelled": "Cancelled",
                  "failed": "Failed", "failed_before_plan": "Failed"}
        # The attempt may have published additional tracks before it stopped.
        observation = await self.inspect(entry.id, entry) if planned else None
        return replace(entry, status=status, diagnostic=diagnostic), View(labels[status], observation)

    async def inspect(self, row_id, entry=None):
        if self.closing:
            return None
        task = asyncio.create_task(jobs.inspect_job(self.store, row_id, enqueued=Path(entry.output) if entry else None))
        self.inspections.add(task)
        try:
            return await task
        except asyncio.CancelledError:
            if not self.closing:
                raise
        finally:
            self.inspections.discard(task)

    async def entry_result(self, entry, result):
        if result.declined:
            diagnostic = "Playlist download declined"
            return replace(entry, status="cancelled", diagnostic=diagnostic), View("Cancelled")
        diagnostic = "\n".join(result.diagnostics)
        if result.destination_changed and not result.output_conflict:
            hint = "interrupted" if entry.status in ("pending", "cancelled") else entry.status
            return replace(entry, status=hint, diagnostic=diagnostic), View("Needs attention", diagnostic=result.attention)
        hint = "failed" if result.failed else "complete"
        observed = await self.inspect(entry.id, entry)
        if result.attention:
            display = View("Needs attention", observed, result.attention)
        elif observed is None or observed.state == "missing":
            display = View("Failed" if result.failed else "Complete")
        else:
            _, display = classify(replace(entry, status=hint), observed, self.store)
        return replace(entry, status=hint, diagnostic=diagnostic), display

    async def run_external(self, row_id, prefs):
        self.operation = asyncio.create_task(self.invoke("external", row_id, prefs))
        try:
            result = await self.operation
            if result is not None:
                self.external_results[row_id] = result
        except (OSError, TimeoutError, YdlError, asyncio.CancelledError) as exc:
            if self.active_id == row_id:
                self.notice = str(exc) or "Download stopped."
        finally:
            self.operation = None
            if self.active_id == row_id:
                self.external[row_id] = await self.inspect(row_id)
                self.plans.pop(row_id, None)
                self.active_id = None
                self.changed()

    async def stop(self, *, quitting=False):
        if quitting:
            self.closing = True
            # Cancel reads separately so in-flight queue writes can still settle.
            for task in self.inspections:
                if not task.cancelling():
                    task.cancel()
        if self.stopping:
            if self.serial is not None:
                await asyncio.shield(self.serial)
            return
        if not (self.busy or quitting):
            return
        self.armed = False
        self.stopping = True
        self.index.clear_notice()
        self.notice = "Stopping…"
        self.changed()
        work = self.serial, self.backend.check
        for task in (self.operation, self.backend.check):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in work if task is not None), return_exceptions=True)
        self.stopping = False
        self.notice = "Queue stopped."
        self.changed()
