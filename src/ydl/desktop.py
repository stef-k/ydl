"""Revalidate saved downloads and dispatch them to the Linux desktop."""

import asyncio
import ctypes
import errno
import fcntl
import os
import signal
import struct
import subprocess
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import jobs
from .models import YdlError

# Linux <sys/inotify.h>. Watch the saved inodes so writes through other hard
# links also invalidate a check, and unrelated directory entries stay irrelevant.
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_CLOSE_WRITE = 0x00000008
IN_OPEN = 0x00000020
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_DONT_FOLLOW = 0x02000000
MEDIA_EVENTS = IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_DELETE_SELF | IN_MOVE_SELF
_libc = ctypes.CDLL(None, use_errno=True)
INOTIFY_EVENT = struct.Struct("=iIII")


@contextmanager
def read_lease(path):
    """Exclude writable handles while updating the watch on this exact inode."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        # SIGURG is ignored by default. A competing write-open can proceed as
        # soon as this short-lived descriptor closes and releases the lease.
        fcntl.fcntl(fd, fcntl.F_SETSIG, signal.SIGURG)
        try:
            fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_RDLCK)
        except BlockingIOError as exc:
            raise YdlError(f"Close the editor or other writer using {path} and try Open again.") from exc
        except OSError as exc:
            raise YdlError(f"Cannot obtain a read lease for {path}: {exc.strerror}.") from exc
        yield f"/proc/self/fd/{fd}"
    finally:
        os.close(fd)


class MediaWatch:
    def __init__(self, events):
        self.events = events
        self.watches = {}
        self.paths = {}
        self.problem = ""
        self.folder = None

    def add(self, path, mask):
        wd = _libc.inotify_add_watch(self.events.fileno(), os.fsencode(path), mask)
        if wd < 0:
            error = ctypes.get_errno()
            if error == errno.ENOSPC:
                raise YdlError("Cannot monitor saved media: the Linux inotify watch limit was reached.")
            raise OSError(error, os.strerror(error), path)
        return wd

    def before_hash(self, path):
        # A writable mapping keeps its underlying file open. If its last writer
        # closes after hashing starts, IN_CLOSE_WRITE invalidates the receipt.
        try:
            self.watches[path] = self.add(path, MEDIA_EVENTS | IN_DONT_FOLLOW)
        except (OSError, YdlError) as exc:
            self.problem = self.problem or str(exc)

    def check_writers(self, path):
        wd = self.watches[path]
        if wd in self.paths:  # Multiple saved names may be hard links.
            return True
        with read_lease(path) as inode:
            if self.add(inode, MEDIA_EVENTS | IN_OPEN) != wd:
                return False
        self.paths[wd] = path
        return True

    def pending_events(self):
        while data := self.events.read(4096):
            offset = 0
            while offset < len(data):
                wd, mask, _, name_length = INOTIFY_EVENT.unpack_from(data, offset)
                offset += INOTIFY_EVENT.size + name_length
                # Directory self-events matter. Its children's own inode
                # watches already cover saved media and exclude unrelated files.
                if wd != self.folder or not name_length:
                    yield wd, mask

    def changed(self):
        with ExitStack() as leases:
            reopened = set()
            for wd, mask in self.pending_events():
                if mask != IN_OPEN:
                    return True
                # An open alone may just be a reader. Pin reopened files
                # while draining the remaining events, including CLOSE_WRITE.
                if wd not in reopened:
                    inode = leases.enter_context(read_lease(self.paths[wd]))
                    if self.add(inode, MEDIA_EVENTS) != wd:
                        return True
                    reopened.add(wd)
        return False


@contextmanager
def media_watch():
    fd = _libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise YdlError(f"Cannot monitor saved media: {os.strerror(ctypes.get_errno())}.")
    with os.fdopen(fd, "rb", buffering=0) as events:
        yield MediaWatch(events)


def download_folder_problem(inspection):
    identity = jobs.inspect_destination(inspection.job)
    if identity is None:
        return "The download folder is missing."
    if identity != inspection.destination_identity:
        return "The download folder has changed."
    return ""


def dispatch_problem(inspection, watch, quitting, *, stop):
    if watch.problem:
        return watch.problem
    for media, stamp in inspection.output_stamps.items():
        if stop.is_set() or quitting.is_set():
            raise asyncio.CancelledError
        if not watch.check_writers(media) or jobs.output_stamp(media) != stamp:
            return f"The saved media is missing or has changed: {media}"
    watch.folder = watch.add(inspection.job.destination, IN_ATTRIB | IN_DELETE_SELF | IN_MOVE_SELF | IN_DONT_FOLLOW)
    problem = download_folder_problem(inspection)
    if quitting.is_set():
        raise asyncio.CancelledError
    return problem


def launch_path_problem(path, inspection):
    if problem := download_folder_problem(inspection):
        return problem
    if path != Path(inspection.job.destination) and jobs.output_stamp(path) != inspection.output_stamps[path]:
        return f"The saved path is missing or has changed: {path}"
    return ""


async def open_in_desktop(path, inspection, watch, *, stop):
    """Dispatch the verified path and report any change through the final launch check."""
    try:
        problem = await jobs.run_file_check(dispatch_problem, inspection, watch, stop)
        if stop.is_set():
            raise asyncio.CancelledError
        if problem:
            return problem
        if problem := launch_path_problem(path, inspection):
            return problem
        if watch.changed():
            return "The saved media changed during verification."
    except (OSError, YdlError) as exc:
        return str(exc)
    finally:
        # Leave no kernel watches allocated during a refresh or the wait for gio.
        watch.events.close()
    try:
        opened = subprocess.Popen(
            ["gio", "open", str(path)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise YdlError("Install gio (libglib2.0-bin on Linux Mint and Ubuntu).") from exc
    try:
        code = await asyncio.to_thread(opened.wait, timeout=10)
    except subprocess.TimeoutExpired as exc:
        opened.kill()
        await asyncio.to_thread(opened.wait)
        raise YdlError("The desktop opener timed out.") from exc
    if code:
        raise YdlError(f"Desktop opener exited with code {code}. Check your desktop session and default app.")
    return ""
