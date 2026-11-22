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

from .config import atomic_write, quality_value
from .models import Settings, YdlError

JOB_ID = re.compile(r"[0-9a-f]{32}")
EXTENSION = re.compile(r"\.[a-zA-Z0-9]{1,12}")


@dataclass
class Item:
    position: int
    video_id: str | None
    title: str
    stem: str
    outputs: dict[str, dict[str, str]] = field(default_factory=dict)
    source: str | None = None


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

    @classmethod
    def create(cls, job_id, settings, items):
        return cls(job_id, time.time_ns(), settings.scope, str(settings.destination),
                   settings.quality, settings.keep_video, items)

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
    if not isinstance(name, str) or not name or name in (".", "..") or Path(name).name != name:
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


class JobStore:
    def __init__(self, state):
        self.directory = state / "jobs"
        self.logs = state / "logs"

    def save(self, job):
        atomic_write(self.directory / f"{job.id}.json", json.dumps(asdict(job), ensure_ascii=True).encode())

    def load(self, job_id):
        if not JOB_ID.fullmatch(job_id):
            raise YdlError("Unknown job ID. Use the ID shown in a previous ydl result.", 2)
        path = self.directory / f"{job_id}.json"
        try:
            fields = json.loads(path.read_bytes())
            items = [Item(**entry) for entry in fields.pop("items")]
            job = Job(items=items, **fields)
            if job.id != job_id or not Path(job.destination).is_absolute():
                raise ValueError("invalid job ID or destination")
            if job.scope not in ("single", "playlist") or type(job.keep_video) is not bool:
                raise ValueError("invalid saved settings")
            quality_value(job.quality)
            return job
        except FileNotFoundError as exc:
            raise YdlError(f"Unknown job ID: {job_id}", 2) from exc
        except (TypeError, KeyError, ValueError, OSError) as exc:
            raise YdlError(f"Cannot read saved job {job_id}: {exc}") from exc

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
    def claim(self, job_id):
        path = self.directory / f"{job_id}.lock"
        with path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise YdlError(f"Job {job_id} is already running. Wait for it to finish.") from exc
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
    return YdlError(f"Output already exists: {path}. Move it aside and retry, or start a new job with --output.")


async def pending_outputs(job, item):
    destination = Path(job.destination)
    names = {"mp3": f"{component(item.stem)}.mp3"}
    if job.keep_video:
        names["video"] = item.outputs.get("video", {}).get("name")
    missing = {}
    for kind, name in names.items():
        path = destination / component(name) if name is not None else None
        if path is None or not os.path.lexists(path):
            missing[kind] = path
            continue
        receipt = item.outputs.get(kind, {})
        try:
            if (receipt.get("name") == name and stat.S_ISREG(path.lstat().st_mode)
                    and await sha256(path) == receipt.get("sha256")):
                continue
        except FileNotFoundError:
            missing[kind] = path
            continue
        raise collision(path)
    return missing


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


async def publish(store, job, item, outputs):
    receipts = {}
    for kind, (staged, target) in outputs.items():
        if os.path.lexists(target):
            raise collision(target)
        receipts[kind] = {"name": target.name, "sha256": await sha256(staged)}
    item.outputs.update(receipts)
    store.save(job)
    for staged, target in outputs.values():
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
