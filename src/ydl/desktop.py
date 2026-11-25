"""Revalidate saved downloads and dispatch them to the Linux desktop."""

import asyncio
import ctypes
import errno
import os
import subprocess
from pathlib import Path

from . import jobs
from .models import YdlError

# Linux <sys/inotify.h>. Watch the saved inodes so writes through other hard
# links also invalidate a check, and unrelated directory entries stay irrelevant.
IN_MODIFY = 0x00000002
IN_ATTRIB = 0x00000004
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
IN_DONT_FOLLOW = 0x02000000
MEDIA_EVENTS = IN_MODIFY | IN_ATTRIB | IN_DELETE_SELF | IN_MOVE_SELF | IN_DONT_FOLLOW
_libc = ctypes.CDLL(None, use_errno=True)


def media_watch():
    fd = _libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise YdlError(f"Cannot monitor saved media: {os.strerror(ctypes.get_errno())}.")
    return os.fdopen(fd, "rb", buffering=0)


def download_folder_problem(inspection):
    identity = jobs.inspect_destination(inspection.job)
    if identity is None:
        return "The download folder is missing."
    if identity != inspection.destination_identity:
        return "The download folder has changed."
    return ""


def dispatch_problem(inspection, watch, quitting, *, stop):
    for media, stamp in inspection.output_stamps.items():
        if stop.is_set() or quitting.is_set():
            raise asyncio.CancelledError
        if _libc.inotify_add_watch(watch.fileno(), os.fsencode(media), MEDIA_EVENTS) < 0:
            error = ctypes.get_errno()
            if error == errno.ENOSPC:
                return f"Cannot monitor {media}: the Linux inotify watch limit was reached."
            raise OSError(error, os.strerror(error), media)
        if jobs.output_stamp(media) != stamp:
            return f"The saved media is missing or has changed: {media}"
    problem = download_folder_problem(inspection)
    if quitting.is_set():
        raise asyncio.CancelledError
    return problem


def launch_path_matches(path, inspection):
    folder = Path(inspection.job.destination)
    stamp = jobs.output_stamp(folder)
    if stamp is None or stamp[:2] != inspection.destination_identity:
        return False
    return path == folder or jobs.output_stamp(path) == inspection.output_stamps[path]


async def open_in_desktop(path, inspection, *, stop):
    """Dispatch the verified path and report any change through the final launch check."""
    with media_watch() as watch:
        try:
            problem = await jobs.run_file_check(dispatch_problem, inspection, watch, stop)
            if stop.is_set():
                raise asyncio.CancelledError
            if problem:
                return problem
            if not launch_path_matches(path, inspection):
                return f"The saved path is missing or has changed: {path}"
            # A nonblocking read returns None if no events are pending. Any event,
            # including a kernel queue overflow, invalidates the worker's result.
            if watch.read(4096) is not None:
                return "The saved media changed during verification."
        except (OSError, YdlError) as exc:
            return str(exc)

        # Quit runs on this loop. Keep its check, event read and spawn in one turn.
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
