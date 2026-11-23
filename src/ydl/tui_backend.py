"""Operational preferences and shared updater readiness for the terminal session."""

import asyncio

from . import backend, config, jobs
from .models import BackendStatus, YdlError
from .process import Log


def backend_key(prefs):
    return prefs.channel, prefs.auto_update


def output_directory(value):
    """Resolve a TUI destination and validate its existing path components."""
    try:
        path = config.output_path(value)
        jobs.safe_destination(path)
    except (OSError, jobs.DestinationChanged) as exc:
        raise config.InvalidOutput(str(exc), 2) from exc
    return path


def save_preferences(path, values):
    output_directory(values.get("output", config.Preferences.output))
    return config.save(path, values)


class BackendReadiness:
    def __init__(self, paths, changed):
        self.paths, self.changed = paths, changed
        self.prefs = config.Preferences()
        self.preferences_lock = asyncio.Lock()
        self.config_error = ""
        self.repair_at_launch = False
        self.status = BackendStatus("Check pending")
        self.ready_key = None
        self.check = None
        self.startup_check_owed = True
        self.log = Log(paths.state / "logs" / "update.log")

    def apply_preferences(self, prefs):
        reload_form = bool(self.config_error)
        if reload_form or backend_key(prefs) != backend_key(self.prefs):
            self.ready_key = None
            self.status = BackendStatus("Check pending")
        self.prefs = prefs
        self.config_error = ""
        self.repair_at_launch = False
        self.changed(defaults=reload_form)

    async def read(self, *, launch=False):
        async with self.preferences_lock:
            try:
                prefs = await asyncio.to_thread(config.load, self.paths.config)
            except (OSError, YdlError) as exc:
                self.config_error = f"Configuration error at {self.paths.config}: {exc}"
                self.repair_at_launch |= launch
                if launch:
                    self.startup_check_owed = False
                self.ready_key = None
                self.status = BackendStatus("Unavailable", detail=self.config_error)
                self.changed()
                return None
            self.apply_preferences(prefs)
            return prefs

    async def save(self, values):
        async with self.preferences_lock:
            prefs = await asyncio.to_thread(save_preferences, self.paths.config, values)
            self.apply_preferences(prefs)
            self.changed(defaults=True)

    def start_check(self):
        self.startup_check_owed = False
        self.status = BackendStatus("Checking", self.status.version)
        self.check = asyncio.create_task(self.run_check(self.prefs))
        self.changed()
        return self.check

    def event_for(self, key, event):
        if event.backend and key == backend_key(self.prefs) and not self.config_error:
            self.status = event.backend
            self.changed()

    async def run_check(self, prefs):
        key = backend_key(prefs)
        try:
            failure = ""
            try:
                await backend.ensure_backend(self.paths.backend, prefs, log=self.log,
                                             emit=lambda event: self.event_for(key, event))
            except (OSError, TimeoutError, YdlError) as exc:
                failure = str(exc) or "Backend check timed out."
            current = await self.read()
            if current is None:
                return False
            if key != backend_key(current):
                return None
            if failure:
                self.ready_key = None
                self.status = BackendStatus("Unavailable", self.status.version, failure)
                return False
            self.ready_key = key
            if self.status.state in ("Checking", "Check pending", "Updating"):
                self.status = BackendStatus("Up to date", self.status.version)
            return True
        except asyncio.CancelledError:
            self.status = BackendStatus("Check pending", self.status.version)
            self.ready_key = None
            raise
        finally:
            self.check = None
            self.changed()

    def executable_stamp(self):
        try:
            info = self.paths.backend.lstat()
        except OSError:
            return None
        return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns

    async def prepare(self, allowed):
        """Return current preferences and the identity of the probed executable."""
        while allowed():
            prefs = await self.read()
            if prefs is None or not allowed():
                return None
            key = backend_key(prefs)
            if self.check is not None:
                admitted = await asyncio.shield(self.check)
                if admitted is False:
                    return None
                continue
            if self.ready_key == key:
                stamp = self.executable_stamp()
                if stamp is not None and await backend.backend_version(self.paths.backend, self.log):
                    current = await self.read()
                    if current is not None and key == backend_key(current) and allowed():
                        return current, stamp
                    continue
            self.ready_key = None
            if not allowed():
                return None
            admitted = await asyncio.shield(self.start_check())
            if admitted is False:
                return None
            if admitted and not await backend.backend_version(self.paths.backend, self.log):
                self.ready_key = None
                self.status = BackendStatus("Unavailable", detail="The managed yt-dlp executable is missing or unusable.")
                self.changed()
                return None
        return None
