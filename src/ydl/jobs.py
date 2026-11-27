"""Persistent download plans and no-overwrite publication."""

import asyncio
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from itertools import count
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event

from .config import admitted_read, atomic_write, quality_value
from .media import ID
from .models import Settings, YdlError
from .process import inherit_lock

JOB_ID = re.compile(r"[0-9a-f]{32}")
EXTENSION = re.compile(r"\.[a-zA-Z0-9]{1,12}")
PLAYLIST_OWNER = ".ydl-playlist-job"
OutputStamp = tuple[int, int, int, int, int]
DirectoryIdentity = tuple[int, int]


class JobBusy(YdlError):
    pass


class MissingPlan(YdlError):
    pass


class OccupiedPlan(YdlError):
    pass


class PlanNeedsSync(YdlError):
    pass


class OutputConflict(YdlError):
    pass


class DestinationChanged(YdlError):
    pass


@dataclass
class Item:
    position: int
    video_id: str | None
    title: str
    stem: str
    outputs: dict[str, dict[str, str]] = field(default_factory=dict)
    source: str | None = None
    video_target: str | None = None


@dataclass
class Job:
    id: str
    created_at: int
    scope: str
    destination: str
    quality: str
    keep_video: bool
    items: list[Item]
    complete: bool = False
    enqueued_base: str | None = None
    owns_playlist_folder: bool = False

    @classmethod
    def create(cls, job_id, settings, items, *, enqueued_base=None, owns_playlist_folder=False):
        return cls(job_id, time.time_ns(), settings.scope, str(settings.destination),
                   settings.quality, settings.keep_video, items, enqueued_base=enqueued_base,
                   owns_playlist_folder=owns_playlist_folder)

    @property
    def settings(self):
        return Settings(self.scope, self.quality, self.keep_video, Path(self.destination))


def safe_name(text, fallback):
    text = unicodedata.normalize("NFC", text)
    clean = "".join(
        "_" if char in '<>:"/\\|?*' or unicodedata.category(char).startswith("C") else char
        for char in text
    ).strip(" .")
    return clean.encode()[:120].decode(errors="ignore").rstrip(" .") or fallback


def make_item(position, video_id, title, playlist):
    label = safe_name(title, "Untitled")
    prefix = f"{position:03d} - " if playlist else ""
    return Item(position, video_id, title, f"{prefix}{label}")


def component(name):
    if not isinstance(name, str) or not name or "\x00" in name or name in (".", "..") or Path(name).name != name:
        raise YdlError("The saved job contains an invalid filename.")
    return name


def local_directory(path):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    _directory_stat(path)
    return path


def _directory_stat(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise YdlError(f"Expected a real directory at {path}. Move the conflicting entry or choose another output.")
    return info


def safe_destination(path, *, remedy="Restore the folder and Recheck."):
    """Check an absolute destination path without resolving redirects."""
    if not path.is_absolute() or ".." in path.parts:
        raise DestinationChanged(f"Invalid saved destination: {path}. {remedy}")
    for folder in (*reversed(path.parents), path):
        try:
            info = folder.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DestinationChanged(f"Cannot access destination {path}: {exc}. {remedy}") from exc
        if not stat.S_ISDIR(info.st_mode) or not os.access(folder, os.R_OK | os.X_OK):
            raise DestinationChanged(f"Cannot use destination {path}: expected a readable real directory at {folder}. "
                                     f"{remedy}")
    return info.st_dev, info.st_ino


def inspect_local_directory(destination):
    try:
        info = _directory_stat(destination)
    except FileNotFoundError:
        return None
    if not os.access(destination, os.R_OK | os.X_OK):
        raise YdlError(f"Cannot access saved destination: {destination}")
    return info.st_dev, info.st_ino


def destination_identity(path, check_ancestors):
    return safe_destination(path) if check_ancestors else inspect_local_directory(path)


@dataclass(frozen=True)
class DestinationGuard:
    identities: dict[Path, DirectoryIdentity]
    playlist_owner: tuple[Path, str] | None = None
    check_ancestors: bool = True

    @classmethod
    def capture(cls, base, destination, *, owner=None, check_ancestors=True):
        identities = {path: destination_identity(path, check_ancestors) for path in (base, destination)}
        guard = cls(identities, (destination, owner) if owner is not None else None, check_ancestors)
        guard.check()
        return guard

    def check(self):
        try:
            for path, expected in self.identities.items():
                if expected is None or destination_identity(path, self.check_ancestors) != expected:
                    raise DestinationChanged(f"Destination changed during the download: {path}. "
                                             "Restore the original folder and Recheck.")
            if self.playlist_owner is not None:
                check_playlist_owner(*self.playlist_owner, required=True)
        except DestinationChanged:
            raise
        except (OSError, YdlError) as exc:
            raise DestinationChanged(str(exc)) from exc


def check_playlist_owner(destination, job_id, *, required):
    marker = destination / PLAYLIST_OWNER
    expected = job_id.encode("ascii")
    try:
        fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if not required:
            return
        reason = "its ydl job marker is missing"
    except OSError as exc:
        raise DestinationChanged(f"Cannot read playlist job marker {marker}: {exc}") from exc
    else:
        try:
            if stat.S_ISREG(os.fstat(fd).st_mode) and os.read(fd, len(expected) + 1) == expected:
                return
        finally:
            os.close(fd)
        reason = "its ydl job marker belongs to another job or is invalid"
    raise DestinationChanged(f"Cannot use playlist folder {destination}: {reason}. "
                             "Restore the original folder, or move this folder aside and retry.")


@contextmanager
def open_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield fd
    finally:
        os.close(fd)


def playlist_base_changed(base):
    return DestinationChanged(f"Destination changed during planning: {base}. Restore the folder and Recheck.")


def place_playlist_folder(staged, destination, base_fd, base_guard, *, numbered):
    for number in count(1):
        candidate = destination if number == 1 else destination.with_name(f"{destination.name} ({number})")
        base_guard.check()
        try:
            rename_without_replace(staged, candidate, parent_fd=base_fd)
        except FileExistsError:
            if not numbered:
                raise
        else:
            return candidate


def claim_playlist_folder(destination, job_id, *, numbered=False, expected_base=None, check_ancestors=True):
    """Publish a marked directory as one operation, relative to the validated parent."""
    base = destination.parent
    base_identity = destination_identity(base, check_ancestors)
    if base_identity is None or (expected_base is not None and base_identity != expected_base):
        raise playlist_base_changed(base)
    base_guard = DestinationGuard({base: base_identity}, check_ancestors=check_ancestors)
    with open_directory(base) as base_fd:
        info = os.fstat(base_fd)
        if (info.st_dev, info.st_ino) != base_identity:
            raise playlist_base_changed(base)
        # Stage under the open parent so a path replacement cannot redirect writes.
        try:
            staging = TemporaryDirectory(dir=f"/proc/self/fd/{base_fd}", prefix=".ydl-playlist-")
        except OSError as exc:
            raise OSError(exc.errno, exc.strerror, base) from exc
        with staging as temporary:
            staged = Path(temporary) / "folder"
            staged.mkdir(mode=0o700)
            atomic_write(staged / PLAYLIST_OWNER, job_id.encode("ascii"))
            with open_directory(staged) as staged_fd:
                info = os.fstat(staged_fd)
                identity = info.st_dev, info.st_ino
                os.fsync(staged_fd)
            claimed = place_playlist_folder(staged, destination, base_fd, base_guard, numbered=numbered)
            os.fsync(base_fd)
    guard = DestinationGuard({base: base_identity, claimed: identity}, (claimed, job_id), check_ancestors)
    guard.check()
    return claimed, guard


def validate_item(item, position):
    if type(item.position) is not int or item.position != position or not isinstance(item.title, str):
        raise ValueError("invalid saved item position or title")
    if item.video_id is not None and not ID.fullmatch(item.video_id):
        raise ValueError("invalid saved video ID")
    component(item.stem)
    for name in (item.source, item.video_target):
        if name is not None:
            component(name)
    for kind, receipt in item.outputs.items():
        if kind not in ("mp3", "video") or not re.fullmatch(r"[0-9a-f]{64}", receipt["sha256"]):
            raise ValueError("invalid saved output receipt")
        component(receipt["name"])


def validate_job(raw, job_id):
    fields = json.loads(raw)
    items = [Item(**entry) for entry in fields.pop("items")]
    job = Job(items=items, **fields)
    destination = Path(job.destination)
    if job.id != job_id or not destination.is_absolute() or "\x00" in job.destination:
        raise ValueError("invalid job ID or destination")
    if job.scope not in ("single", "playlist") or type(job.keep_video) is not bool:
        raise ValueError("invalid saved settings")
    if type(job.owns_playlist_folder) is not bool or (job.owns_playlist_folder and job.scope != "playlist"):
        raise ValueError("invalid playlist folder ownership setting")
    if type(job.created_at) is not int or type(job.complete) is not bool or not items:
        raise ValueError("invalid job timestamp, completion flag or item list")
    if job.enqueued_base is not None:
        base = Path(job.enqueued_base)
        expected = destination if job.scope == "single" else destination.parent
        if not base.is_absolute() or base != expected:
            raise ValueError("invalid enqueued base directory")
    quality_value(job.quality)
    for position, item in enumerate(items, 1):
        validate_item(item, position)
    return job


class JobStore:
    def __init__(self, state):
        self.directory = state / "jobs"
        self.logs = state / "logs"

    def path(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise YdlError("Unknown job ID. Use the ID shown in a previous ydl result.", 2)
        return self.directory / f"{job_id}.json"

    def save(self, job):
        fields = asdict(job)
        if not job.owns_playlist_folder:
            del fields["owns_playlist_folder"]
        atomic_write(self.path(job.id), json.dumps(fields, ensure_ascii=True).encode(), durable=True)

    def load(self, job_id, *, admit=False):
        path = self.path(job_id)
        try:
            return admitted_read(path, lambda raw: validate_job(raw, job_id)) if admit else validate_job(path.read_bytes(), job_id)
        except FileNotFoundError as exc:
            if not os.path.lexists(path):
                raise MissingPlan(f"Unknown job ID: {job_id}. Expected saved plan at {path}.", 2) from exc
            raise PlanNeedsSync(f"Cannot read saved job at {path}: {exc}") from exc
        except (AttributeError, TypeError, KeyError, ValueError, OSError, YdlError) as exc:
            raise PlanNeedsSync(f"Cannot read or sync saved job at {path}: {exc}") from exc

    def select(self, job_id):
        if job_id is not None:
            return self.load(job_id)
        jobs = (self.load(path.stem) for path in self.directory.glob("*.json") if JOB_ID.fullmatch(path.stem))
        job = max((job for job in jobs if not job.complete),
                  key=lambda job: (job.created_at, job.id), default=None)
        if job is None:
            raise YdlError("No incomplete ydl job is available.", 2)
        return job

    def recent_ids(self, limit=20):
        # The creation timestamp is immutable across atomic plan replacements.
        # The separate inspector admits the plan itself under its shared lock.
        ranked = []
        for path in self.directory.glob("*.json"):
            if JOB_ID.fullmatch(path.stem):
                ranked.append((self.created_at(path), path.stem))
        return [job_id for _, job_id in sorted(ranked, reverse=True)[:limit]]

    @staticmethod
    def created_at(path):
        try:
            fields = json.loads(path.read_bytes())
            timestamp = fields.get("created_at")
            return timestamp if type(timestamp) is int else 0
        except (OSError, UnicodeError, ValueError, AttributeError):
            return 0

    @contextmanager
    def claim(self, job_id, *, new=False):
        path = self.path(job_id)
        lock_path = path.with_suffix(".lock")
        if not new and not (os.path.lexists(path) or os.path.lexists(lock_path)):
            raise MissingPlan(f"Unknown job ID: {job_id}. Expected saved plan at {path}.", 2)
        self.directory.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise JobBusy(f"Job already running: {job_id}. Wait for it to finish.") from exc
            with inherit_lock(lock.fileno()):
                yield

    @contextmanager
    def observe(self, job_id):
        path = self.path(job_id).with_suffix(".lock")
        try:
            lock = path.open("r")
        except FileNotFoundError as exc:
            if not os.path.lexists(self.path(job_id)):
                raise MissingPlan(str(self.path(job_id))) from exc
            raise PlanNeedsSync(f"Missing job lock: {path}. Use ydl retry {job_id} to acquire it.") from exc
        with lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise JobBusy(f"Job already running: {job_id}. Wait for it to finish.") from exc
            yield


async def run_file_check(check, *args):
    """Run a cancellable filesystem check and join it before releasing caller-owned resources."""
    stop = Event()
    worker = asyncio.create_task(asyncio.to_thread(check, *args, stop=stop))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Keep the caller's job lock until any in-flight file read has finished.
        stop.set()
        await asyncio.gather(worker, return_exceptions=True)
        raise


def _sha256(path, *, stop):
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise YdlError(f"Cannot verify a non-regular file: {path}")
        while not stop.is_set():
            if not (data := stream.read(1024 * 1024)):
                return digest.hexdigest()
            digest.update(data)
    raise asyncio.CancelledError


def collision(path):
    return OutputConflict(f"Output already exists: {path}. Move it aside and retry, or start a new job with --output.")


async def pending_outputs(job, item):
    missing, _, error = await run_file_check(_check_item_outputs, job, item)
    if error is not None:
        raise error
    return missing


def _check_item_outputs(job, item, *, stop, before_hash=None):
    if stop.is_set():
        raise asyncio.CancelledError
    destination = Path(job.destination)
    names = {"mp3": f"{component(item.stem)}.mp3"}
    if job.keep_video:
        names["video"] = item.outputs.get("video", {}).get("name", item.video_target)
    missing = {}
    verified = {}
    try:
        for kind, name in names.items():
            path = destination / component(name) if name is not None else None
            stamp = _verify_output(path, item.outputs.get(kind, {}), stop, before_hash) if path is not None else None
            if stamp is None:
                missing[kind] = path if kind == "mp3" or kind in item.outputs else None
                continue
            verified[kind] = path, stamp

        # The MP3 may have changed while we hashed the retained video.
        for kind, (path, stamp) in tuple(verified.items()):
            current = output_stamp(path)
            if current == stamp:
                continue
            del verified[kind]
            if current is not None:
                raise collision(path)
            missing[kind] = path
    except (OSError, YdlError) as exc:
        return missing, verified, exc
    return missing, verified, None


def _file_stamp(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def output_stamp(path):
    """Read the identity and change times of an output without following symlinks."""
    try:
        return _file_stamp(path.lstat())
    except FileNotFoundError:
        return None


def _verify_output(path, receipt, stop, before_hash):
    try:
        info = path.lstat()
        if receipt.get("name") == path.name and stat.S_ISREG(info.st_mode):
            if before_hash is not None:
                before_hash(path)
            if _sha256(path, stop=stop) == receipt.get("sha256"):
                return _file_stamp(info)
    except FileNotFoundError:
        return None
    raise collision(path)


@dataclass(frozen=True)
class Inspection:
    state: str
    job: Job | None = None
    verified: int = 0
    output: Path | None = None
    diagnostic: str = ""
    destination_identity: tuple[int, int] | None = None
    # Dispatch uses these stamps without treating a metadata change as a new job status.
    output_stamps: dict[Path, OutputStamp] = field(default_factory=dict, compare=False, repr=False)


def inspect_destination(job):
    """Validate the saved folder and return its identity when present."""
    destination = Path(job.destination)
    if job.enqueued_base is not None:
        safe_destination(Path(job.enqueued_base))
    identity = destination_identity(destination, job.enqueued_base is not None)
    if identity is not None and job.scope == "playlist":
        check_playlist_owner(destination, job.id, required=job.owns_playlist_folder)
    return identity


def resume_destination(job):
    destination = Path(job.destination)
    base = destination.parent
    identity = inspect_destination(job)
    queued = job.enqueued_base is not None
    if job.owns_playlist_folder:
        if identity is None:
            base.mkdir(parents=True, exist_ok=True)
            try:
                _, guard = claim_playlist_folder(destination, job.id, check_ancestors=queued)
            except FileExistsError:
                pass  # Check the folder that won the path before using it.
            else:
                return guard
        return DestinationGuard.capture(base, destination, owner=job.id, check_ancestors=queued)

    base.mkdir(parents=True, exist_ok=True)
    local_directory(destination)
    if job.scope == "playlist":
        inspect_destination(job)
    if queued:
        return DestinationGuard.capture(Path(job.enqueued_base), destination)


async def inspect_job(store, job_id, *, enqueued=None, before_hash=None):
    """Inspect under the retry lock, calling before_hash(path) before each receipt read."""
    return await run_file_check(_inspect_job, store, job_id, enqueued, before_hash)


def _inspect_job(store, job_id, enqueued, before_hash, *, stop):
    job = None
    try:
        with store.observe(job_id):
            job = store.load(job_id)
            if job.enqueued_base is not None or enqueued is not None:
                job = None
                job = store.load(job_id, admit=True)
            if enqueued is not None:
                safe_destination(enqueued)
            return _inspect_outputs(job, stop=stop, before_hash=before_hash)
    except MissingPlan:
        if enqueued is not None:
            try:
                safe_destination(enqueued)
            except DestinationChanged as exc:
                return Inspection("blocked", diagnostic=str(exc))
        return Inspection("missing")
    except (OSError, YdlError) as exc:
        return Inspection("blocked", job, diagnostic=str(exc))


def _inspect_outputs(job, *, stop, before_hash=None):
    output = Path(job.destination) if job.scope == "playlist" else None
    destination_identity = inspect_destination(job)
    if destination_identity is None:
        return Inspection("unfinished", job, output=output)
    verified = 0
    diagnostic = ""
    output_stamps = {}
    for item in job.items:
        missing, item_outputs, error = _check_item_outputs(job, item, stop=stop, before_hash=before_hash)
        if job.scope == "single" and item.position == 1 and "mp3" in item_outputs:
            output = item_outputs["mp3"][0]
        if error is not None:
            diagnostic = diagnostic or str(error)
        elif not missing and item.video_id:
            verified += 1
            output_stamps.update(item_outputs.values())
    state = "blocked" if diagnostic else "complete" if verified == len(job.items) else "unfinished"
    return Inspection(state, job, verified, output, diagnostic, destination_identity,
                      output_stamps=output_stamps if state == "complete" else {})


def staging_path(job, item):
    destination = Path(job.destination)
    root = local_directory(destination / ".ydl-partials")
    directory = local_directory(root / job.id)
    return local_directory(directory / str(item.position))


def find_source(staging, item):
    if item.source is not None:
        path = staging / component(item.source)
        if path.is_file() and not path.is_symlink():
            return path
    return None


def video_suffix(path):
    if not EXTENSION.fullmatch(path.suffix):
        raise YdlError("yt-dlp returned an unsupported video filename extension.")
    return path.suffix


async def publish(store, job, item, outputs, guard=None):
    if guard:
        guard.check()
    receipts = {}
    for kind, (staged, target) in outputs.items():
        if os.path.lexists(target):
            raise collision(target)
        receipts[kind] = {"name": target.name, "sha256": await run_file_check(_sha256, staged)}
    item.outputs.update(receipts)
    store.save(job)
    for staged, target in outputs.values():
        if guard:
            guard.check()
        try:
            publish_file(staged, target)
        except FileExistsError as exc:
            raise collision(target) from exc


def publish_file(staged, target):
    try:
        os.link(staged, target, follow_symlinks=False)
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS):
            raise
        try:
            rename_without_replace(staged, target)
        except OSError as rename_error:
            raise rename_error from exc


def rename_without_replace(staged, target, *, parent_fd=None):
    at_fdcwd, rename_noreplace = -100, 1
    target_fd, name = (at_fdcwd, target) if parent_fd is None else (parent_fd, target.name)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(at_fdcwd, os.fsencode(staged), target_fd, os.fsencode(name), rename_noreplace):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def remove_staging(staging):
    shutil.rmtree(staging)
    for directory in (staging.parent, staging.parent.parent):
        try:
            directory.rmdir()
        except OSError:
            # Other items or jobs can still be using the shared parent.
            break
