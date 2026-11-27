"""Claim playlist folders on Linux filesystems without no-replace rename."""

import fcntl
import os
import stat
from contextlib import contextmanager

from . import file_events
from .models import DestinationChanged

PLAYLIST_OWNER = ".ydl-playlist-job"
CLAIMS = ".ydl-playlist-claims"

# Child name removals and moves, plus loss of the directory being watched.
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
IN_DELETE = 0x00000200
IN_DELETE_SELF = 0x00000400
IN_MOVE_SELF = 0x00000800
CLAIM_EVENTS = IN_MOVED_FROM | IN_MOVED_TO | IN_DELETE | IN_DELETE_SELF | IN_MOVE_SELF


@contextmanager
def open_directory(path, *, access=os.O_RDONLY, parent_fd=None):
    fd = os.open(path, access | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def claim_pending(folder):
    try:
        (folder.parent / CLAIMS / folder.name).lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


@contextmanager
def locked_claims(base_fd):
    try:
        os.mkdir(CLAIMS, mode=0o700, dir_fd=base_fd)
    except FileExistsError:
        pass
    with open_directory(CLAIMS, parent_fd=base_fd) as claims_fd:
        # Serialize fallback claims only until the ownership marker is ready.
        fcntl.flock(claims_fd, fcntl.LOCK_EX)
        yield claims_fd


def reserve_name(name, claims_fd):
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=claims_fd)
    except FileExistsError:
        info = os.stat(name, dir_fd=claims_fd, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or info.st_size:
            raise DestinationChanged(f"Invalid pending playlist claim for {name}.")
    else:
        os.close(fd)


def claim_exclusive_folder(folder, base_fd, job_id):
    """Return the claimed inode, or None when the candidate name is occupied."""
    name = folder.name
    with open_directory(".", parent_fd=base_fd) as sync_fd, locked_claims(base_fd) as claims_fd:
        try:
            os.stat(name, dir_fd=base_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            return None

        with file_events.open_events() as events:
            file_events.add_watch(events, f"/proc/self/fd/{base_fd}", CLAIM_EVENTS)
            # Keep this record if creation is interrupted before the marker is durable.
            # An absent folder can reuse it after taking the lock.
            reserve_name(name, claims_fd)
            os.fsync(claims_fd)
            os.fsync(sync_fd)
            try:
                os.mkdir(name, mode=0o700, dir_fd=base_fd)
            except FileExistsError:
                os.unlink(name, dir_fd=claims_fd)
                return None
            identity = mark_claimed_folder(folder, base_fd, job_id, events)
        os.fsync(sync_fd)
        os.unlink(name, dir_fd=claims_fd)
        os.fsync(claims_fd)
        return identity


def mark_claimed_folder(folder, base_fd, job_id, events):
    name = folder.name
    folded_name = name.casefold()
    try:
        with open_directory(name, parent_fd=base_fd) as folder_fd:
            # The same-name rename leaves the entry in place and takes the parent
            # directory lock, so an earlier move has finished sending its event.
            os.rename(name, name, src_dir_fd=base_fd, dst_dir_fd=base_fd)
            if any(not changed or os.fsdecode(changed).casefold() == folded_name
                   for _, _, changed in file_events.pending_events(events)):
                raise folder_changed(folder)
            fd = os.open(PLAYLIST_OWNER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         mode=0o600, dir_fd=folder_fd)
            with os.fdopen(fd, "wb") as stream:
                stream.write(job_id.encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
            os.fsync(folder_fd)
            info = os.fstat(folder_fd)
            return info.st_dev, info.st_ino
    except (FileNotFoundError, NotADirectoryError, FileExistsError) as exc:
        raise folder_changed(folder) from exc


def folder_changed(folder):
    return DestinationChanged(f"Playlist folder changed while being claimed: {folder}. "
                              "Restore the original folder and Recheck.")
