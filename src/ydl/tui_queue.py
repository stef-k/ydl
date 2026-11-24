"""The versioned queue index and its local reconciliation with saved jobs."""

import asyncio
import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import UUID, uuid4

from . import config, jobs, media
from .models import Result, YdlError
from .process import inherit_lock

STARTABLE = {"Pending", "Interrupted"}
HINTS = {"pending", "active", "complete", "failed", "failed_before_plan", "cancelled", "interrupted"}


@dataclass(frozen=True)
class Entry:
    id: str
    url: str
    scope: str
    quality: str
    keep_video: bool
    output: str
    status: str = "pending"
    diagnostic: str = ""

    @classmethod
    def from_url(cls, url, scope, quality, keep_video, output):
        address = media.parse_url(url)
        media.validate_scope(address, scope)
        return cls(uuid4().hex, address.canonical(), scope, quality, keep_video, str(output))

    def validate(self):
        identifier = UUID(self.id)
        if identifier.version != 4 or identifier.hex != self.id:
            raise ValueError("invalid queue entry ID")
        address = media.parse_url(self.url)
        if self.url != address.canonical() or self.scope not in ("auto", "single", "playlist"):
            raise ValueError("invalid queue URL or scope")
        media.validate_scope(address, self.scope)
        config.quality_value(self.quality)
        output = Path(self.output)
        if not output.is_absolute() or ".." in output.parts or "\x00" in self.output:
            raise ValueError("invalid queue destination")
        if type(self.keep_video) is not bool or self.status not in HINTS or not isinstance(self.diagnostic, str):
            raise ValueError("invalid queue options or state")


def decode_queue(raw):
    data = json.loads(raw)
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported queue schema version")
    if not isinstance(data.get("entries"), list):
        raise ValueError("queue entries must be a list")
    entries = [Entry(**fields) for fields in data["entries"]]
    for entry in entries:
        entry.validate()
    if len({entry.id for entry in entries}) != len(entries):
        raise ValueError("duplicate queue entry ID")
    return entries


class QueueFile:
    def __init__(self, state):
        self.path = state / "tui-queue.json"

    @contextmanager
    def own(self):
        lock_path = self.path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise YdlError(f"Queue is busy: {self.path}. Wait for the other ydl session to finish.") from exc
            with inherit_lock(lock.fileno()):
                yield

    def read(self, *, allow_missing=False):
        try:
            return config.admitted_read(self.path, decode_queue)
        except FileNotFoundError:
            if allow_missing and not os.path.lexists(self.path):
                return None
            raise

    def save(self, entries):
        raw = json.dumps({"schema_version": 1, "entries": [asdict(entry) for entry in entries]})
        config.atomic_write(self.path, raw.encode(), durable=True)


@dataclass(frozen=True)
class View:
    status: str
    inspection: jobs.Inspection | None = None
    diagnostic: str = ""
    result: Result | None = None

    @property
    def planned(self):
        return self.inspection is not None and self.inspection.job is not None


def missing_plan_message(store, row_id, *, check_again="Recheck"):
    return (f"Missing saved plan at {store.path(row_id)}. Restore it and {check_again}, "
            "or use Add to start fresh with a new ID.")


def planned_hint(hint):
    if hint == "failed_before_plan":
        return "failed"
    if hint in ("pending", "cancelled"):
        return "interrupted"
    return hint


def classify(entry, inspection, store):
    hint = entry.status
    if inspection.state == "complete":
        return "complete", View("Complete", inspection)
    if inspection.job is not None:
        hint = planned_hint(hint)
    if inspection.state == "blocked":
        return hint, View("Needs attention", inspection, inspection.diagnostic)
    if inspection.state == "unfinished":
        return hint, View("Failed" if hint == "failed" else "Interrupted", inspection)
    status = {"pending": "Pending", "cancelled": "Cancelled", "failed_before_plan": "Failed"}.get(hint)
    if status is None:
        return hint, View("Needs attention", diagnostic=missing_plan_message(store, entry.id))
    return hint, View(status)


def with_result(rows, result):
    return [result if row.id == result.id else row for row in rows]


class QueueIndex:
    """Serialize mutations without running a queue fsync on the UI thread."""

    def __init__(self, disk, store, changed, inspect):
        self.disk, self.store, self.changed = disk, store, changed
        self.inspect = inspect
        self.rows = []
        self.views = {}
        self.buffer = {}
        self.lock = asyncio.Lock()
        self.admitted = False
        self.verifying = False
        self.recovering = False
        self.error = ""
        self.notice = ""
        self.restore_notice = ""
        self._restore_notice_dismissed = False

    def clear_notice(self):
        self.notice = ""
        self.dismiss_restore_notice()

    def dismiss_restore_notice(self):
        """Dismiss the notice even if startup verification is still running."""
        self.restore_notice = ""
        self._restore_notice_dismissed = True

    def row(self, row_id):
        return next((entry for entry in self.rows if entry.id == row_id), None)

    def view(self, row_id):
        return self.views.get(row_id, View("Pending"))

    @property
    def editable(self):
        return self.admitted and not self.error and not self.recovering

    @property
    def runnable(self):
        return self.editable and not self.verifying

    async def open(self, on_admitted):
        rows = await self.admit(allow_missing=True)
        if not self.admitted:
            return
        on_admitted()
        if await self.recheck(startup=True) and rows is not None and not self._restore_notice_dismissed:
            pending = sum(self.view(row.id).status == "Pending" for row in self.rows)
            interrupted = sum(self.view(row.id).status == "Interrupted" for row in self.rows)
            self.restore_notice = f"Queue restored · {pending} Pending · {interrupted} Interrupted"
            self.changed()

    async def admit(self, *, allow_missing=False):
        try:
            rows = await asyncio.to_thread(self.disk.read, allow_missing=allow_missing)
        except (OSError, AttributeError, TypeError, ValueError, KeyError, YdlError) as exc:
            self.error = (f"Cannot admit queue: {exc}.\nQueue: {self.disk.path}\n"
                          "Restore a valid copy and choose Recover queue.\n"
                          "Or move the file aside and reopen.\n"
                          "Saved jobs also support ydl retry JOB_ID.")
            self.changed()
            return None
        self.rows = rows or []
        self.views = {row.id: View("Verifying") for row in self.rows}
        self.verifying = bool(self.rows)
        self.admitted = True
        self.changed()
        return rows

    async def save(self, rows, *, required=False, result=None):
        """Save under the caller's mutation lock."""
        if rows == self.rows:
            return True
        try:
            await asyncio.to_thread(self.disk.save, rows)
        except OSError as exc:
            uncertain = getattr(exc, "replaced", False)
            message = "Save durability uncertain" if uncertain else "Save rejected"
            self.notice = f"{message} for {self.disk.path}: {exc}"
            if required or uncertain:
                self.error = self.notice
                if result is not None:
                    self.buffer[result.id] = result
            self.changed()
            return False
        self.rows = rows
        self.changed()
        return True

    async def edit(self, transform):
        async with self.lock:
            if not self.editable:
                return False
            self.clear_notice()
            rows = transform(self.rows)
            if rows is None:
                return False
            if not await self.save(rows):
                return False
            self.views = {row.id: self.view(row.id) for row in self.rows}
            self.changed()
            return True

    async def recheck(self, row_id=None, *, startup=False):
        targets = [row for row in self.rows if row_id is None or row.id == row_id]
        self.verifying = True
        if not startup:
            self.clear_notice()
        self.views.update({row.id: View("Verifying") for row in targets})
        self.changed()
        try:
            updates = await asyncio.gather(*(self.verify(row) for row in targets))
            if any(row is None for row in updates):
                return False
            hints = {row.id: row for row in updates}
            async with self.lock:
                if self.error and not self.recovering:
                    return False
                return await self.save([hints.get(row.id, row) for row in self.rows], required=True)
        finally:
            self.verifying = False
            self.changed()

    async def verify(self, entry):
        observation = await self.inspect(entry.id, entry)
        if observation is None:
            return None
        hint, view = classify(entry, observation, self.store)
        if self.row(entry.id) is not None:
            self.views[entry.id] = view
            self.changed()
        diagnostic = entry.diagnostic
        # Older versions saved the missing-plan notice as the last attempt diagnostic.
        if observation.job is not None and diagnostic == missing_plan_message(self.store, entry.id):
            diagnostic = ""
        return replace(entry, status=hint, diagnostic=diagnostic)

    async def recover(self):
        self.recovering = True
        self.changed()
        try:
            if await self.admit() is None:
                return False
            orphans = [row for row in self.buffer.values() if self.row(row.id) is None]
            rows = [self.buffer.get(row.id, row) for row in self.rows]
            async with self.lock:
                if not await self.save(rows, required=True):
                    return False
                self.buffer = {row.id: row for row in orphans}
            if not await self.recheck():
                return False
            self.buffer.clear()
            self.error = ""
            self.notice = "Queue recovered. The queue is idle."
            for row in orphans:
                self.notice += f" Job {row.id}: {row.status}. {row.diagnostic}"
            return True
        finally:
            self.recovering = False
            self.changed()

    async def activate(self, entry):
        """Save Active under the caller's mutation lock."""
        self.clear_notice()
        prior = self.view(entry.id)
        current = replace(entry, status="active")
        if not await self.save(with_result(self.rows, current), required=True, result=entry):
            return None
        self.views[entry.id] = View("Active", prior.inspection)
        self.changed()
        return prior

    async def settle(self, result, view):
        async with self.lock:
            if self.error:
                self.buffer[result.id] = result
            elif await self.save(with_result(self.rows, result), required=True, result=result):
                self.views[result.id] = view
            self.changed()
