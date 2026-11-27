"""Read Linux inotify events from a nonblocking descriptor."""

import ctypes
import os
import struct

libc = ctypes.CDLL(None, use_errno=True)
EVENT = struct.Struct("=iIII")


def open_events():
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return os.fdopen(fd, "rb", buffering=0)


def add_watch(events, path, mask):
    watch = libc.inotify_add_watch(events.fileno(), os.fsencode(path), mask)
    if watch < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), path)
    return watch


def pending_events(events):
    while data := events.read(4096):
        offset = 0
        while offset < len(data):
            watch, mask, _, length = EVENT.unpack_from(data, offset)
            start = offset + EVENT.size
            offset = start + length
            yield watch, mask, data[start:offset].split(b"\0", 1)[0]
