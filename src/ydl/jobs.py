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
from pathlib import Path

from .config import admitted_read, atomic_write, quality_value
from .media import ID
from .models import Settings, YdlError
from .process import inherit_lock

JOB_ID = re.compile(r"[0-9a-f]{32}")
EXTENSION = re.compile(r"\.[a-zA-Z0-9]{1,12}")


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

    @classmethod
    def create(cls, job_id, settings, items, *, enqueued_base=None):
        return cls(job_id, time.time_ns(), settings.scope, str(settings.destination),
                   settings.quality, settings.keep_video, items, enqueued_base=enqueued_base)

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
    stem = f"{prefix}{label} [{video_id}]" if video_id else f"{prefix}{label}"
    return Item(position, video_id, title, stem)


def component(name):
    if not isinstance(name, str) or not name or "\x00" in name or name in (".", "..") or Path(name).name != name:
        raise YdlError("The saved job contains an invalid filename.")
    return name


def local_directory(path, *, create=True):
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise YdlError(f"Expected a real directory at {path}. Move the conflicting entry or choose another output.")
    return path


def safe_destination(path):
    """Check an enqueued absolute path without resolving redirects."""
    if not path.is_absolute() or ".." in path.parts:
        raise DestinationChanged(f"Invalid saved destination: {path}. Restore the folder and Recheck.")
    for folder in (*reversed(path.parents), path):
        try:
            info = folder.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DestinationChanged(f"Cannot access destination {path}: {exc}. Restore the folder and Recheck.") from exc
        if not stat.S_ISDIR(info.st_mode) or not os.access(folder, os.R_OK | os.X_OK):
            raise DestinationChanged(f"Cannot use destination {path}: expected a readable real directory at {folder}. "
                                     "Restore the folder and Recheck.")
    return info.st_dev, info.st_ino


@dataclass(frozen=True)
class DestinationGuard:
    identities: dict[Path, tuple[int, int]]

    @classmethod
    def capture(cls, base, destination):
        guard = cls({path: safe_destination(path) for path in (base, destination)})
        guard.check()
        return guard

    def check(self):
        for path, expected in self.identities.items():
            if expected is None or safe_destination(path) != expected:
                raise DestinationChanged(f"Destination changed during the download: {path}. "
                                         "Restore the original folder and Recheck.")


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
        atomic_write(self.path(job.id), json.dumps(asdict(job), ensure_ascii=True).encode(), durable=True)

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

    @contextmanager
    def claim(self, job_id, *, new=False):
        path = self.path(job_id)
        if not new and not os.path.lexists(path):
            raise MissingPlan(f"Unknown job ID: {job_id}. Expected saved plan at {path}.", 2)
        self.directory.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a") as lock:
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


async def sha256(path):
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise YdlError(f"Cannot verify a non-regular file: {path}")
        while data := stream.read(1024 * 1024):
            digest.update(data)
            await asyncio.sleep(0)
    return digest.hexdigest()


def collision(path):
    return OutputConflict(f"Output already exists: {path}. Move it aside and retry, or start a new job with --output.")


async def pending_outputs(job, item):
    destination = Path(job.destination)
    names = {"mp3": f"{component(item.stem)}.mp3"}
    if job.keep_video:
        names["video"] = item.outputs.get("video", {}).get("name", item.video_target)
    missing = {}
    for kind, name in names.items():
        path = destination / component(name) if name is not None else None
        if path is None or not os.path.lexists(path):
            missing[kind] = path if kind == "mp3" or kind in item.outputs else None
            continue
        receipt = item.outputs.get(kind, {})
        try:
            if (receipt.get("name") == name and stat.S_ISREG(path.lstat().st_mode)
                    and await sha256(path) == receipt.get("sha256")):
                continue
        except FileNotFoundError:
            missing[kind] = path if kind == "mp3" or kind in item.outputs else None
            continue
        raise collision(path)
    return missing


@dataclass(frozen=True)
class Inspection:
    state: str
    job: Job | None = None
    verified: int = 0
    output: Path | None = None
    diagnostic: str = ""


def inspect_destination(job):
    destination = Path(job.destination)
    if job.enqueued_base is not None:
        safe_destination(Path(job.enqueued_base))
        return safe_destination(destination) is not None
    try:
        local_directory(destination, create=False)
    except FileNotFoundError:
        return False
    if not os.access(destination, os.R_OK | os.X_OK):
        raise YdlError(f"Cannot access saved destination: {destination}")
    return True


async def inspect_job(store, job_id, *, enqueued=None):
    """Inspect the plan and its receipts under the same lock used for retry."""
    job = None
    try:
        with store.observe(job_id):
            job = store.load(job_id)
            if job.enqueued_base is not None or enqueued is not None:
                job = store.load(job_id, admit=True)
            if enqueued is not None:
                safe_destination(enqueued)
            return await inspect_outputs(job)
    except MissingPlan:
        if enqueued is not None:
            try:
                safe_destination(enqueued)
            except DestinationChanged as exc:
                return Inspection("blocked", diagnostic=str(exc))
        return Inspection("missing")
    except (OSError, YdlError) as exc:
        return Inspection("blocked", job, diagnostic=str(exc))


async def inspect_outputs(job):
    if not inspect_destination(job):
        return Inspection("unfinished", job)
    verified = 0
    diagnostic = ""
    for item in job.items:
        try:
            if not await pending_outputs(job, item) and item.video_id:
                verified += 1
        except (OSError, YdlError) as exc:
            diagnostic = diagnostic or str(exc)
    output = Path(job.destination)
    if job.scope == "single":
        output = output / f"{job.items[0].stem}.mp3" if verified else None
    state = "blocked" if diagnostic else "complete" if verified == len(job.items) else "unfinished"
    return Inspection(state, job, verified, output, diagnostic)


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
        receipts[kind] = {"name": target.name, "sha256": await sha256(staged)}
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
        # Linux supports atomic no-replace rename on filesystems without hard links.
        at_fdcwd, rename_noreplace = -100, 1
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.renameat2(at_fdcwd, os.fsencode(staged), at_fdcwd, os.fsencode(target), rename_noreplace):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), target) from exc


def remove_staging(staging):
    shutil.rmtree(staging)
    for directory in (staging.parent, staging.parent.parent):
        try:
            directory.rmdir()
        except OSError:
            # Other items or jobs can still be using the shared parent.
            break
