"""Preferences and user-writable paths."""

import json
import os
import re
import tempfile
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .models import YdlError


@dataclass(frozen=True)
class Paths:
    config: Path
    data: Path
    state: Path

    @classmethod
    def discover(cls):
        home = Path.home()

        def locate(name, fallback):
            value = Path(os.environ.get(name, ""))
            return (value if value.is_absolute() else home / fallback) / "ydl"

        return cls(
            locate("XDG_CONFIG_HOME", ".config") / "config.toml",
            locate("XDG_DATA_HOME", ".local/share"),
            locate("XDG_STATE_HOME", ".local/state"),
        )

    @property
    def backend(self):
        return self.data / "bin" / "yt-dlp"


@dataclass(frozen=True)
class Preferences:
    output: str = "~/Downloads/ydl"
    quality: str = "0"
    keep_video: bool = False
    auto_update: bool = True
    channel: str = "nightly"
    remove_on_success: bool = False


def quality_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"(?:[0-9]|[1-9][0-9]*[Kk])", value):
        raise YdlError("Quality must be a VBR setting from 0 to 9 or a bitrate such as 192K.", 2)
    return value.upper()


class InvalidOutput(YdlError):
    """An output preference could not be resolved to a path."""


def output_path(value):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise InvalidOutput("Output must name a directory.", 2)
    try:
        return Path(value).expanduser().resolve()
    except (RuntimeError, ValueError) as exc:
        raise InvalidOutput(f"Invalid output directory: {exc}", 2) from exc


def validate(values):
    for key, value in values.items():
        if key not in Preferences.__dataclass_fields__:
            raise YdlError(f"Unknown configuration key: {key}", 2)
        if key in ("keep_video", "auto_update", "remove_on_success") and type(value) is not bool:
            raise YdlError(f"{key} must be true or false.", 2)
    prefs = Preferences(**values)
    if prefs.channel not in ("stable", "nightly"):
        raise YdlError("Channel must be stable or nightly.", 2)
    output_path(prefs.output)
    return Preferences(**{**asdict(prefs), "quality": quality_value(prefs.quality)})


def load(path):
    try:
        with path.open("rb") as stream:
            return validate(tomllib.load(stream))
    except FileNotFoundError:
        return Preferences()
    except (OSError, ValueError) as exc:
        raise YdlError(f"Cannot read configuration {path}: {exc}", 2) from exc


def toml_text(prefs):
    def literal(value):
        return str(value).lower() if isinstance(value, bool) else json.dumps(value, ensure_ascii=False)

    return "".join(f"{key} = {literal(value)}\n" for key, value in asdict(prefs).items())


def set_value(path, prefs, key, value):
    if key == "keep_video":
        if value not in ("true", "false"):
            raise YdlError("keep_video must be true or false.", 2)
        value = value == "true"
    return save(path, {**asdict(prefs), key: value})


def save(path, values):
    updated = validate(values)
    atomic_write(path, toml_text(updated).encode())
    return updated


class SaveError(OSError):
    def __init__(self, path, cause, *, replaced):
        super().__init__(f"{path}: {cause}")
        self.replaced = replaced


def sync_directories(path):
    # Include ancestors even when they already exist: an earlier interrupted
    # save could have created them without syncing their directory entries.
    for directory in (path, *path.parents):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def admitted_read(path, validate_bytes):
    with path.open("rb") as stream:
        value = validate_bytes(stream.read())
        os.fsync(stream.fileno())
    sync_directories(path.parent)
    return value


def atomic_write(path, data, mode=0o600, *, durable=False):
    replaced = False
    staged = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            staged = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        staged.replace(path)
        replaced = True
        if durable:
            sync_directories(path.parent)
    except OSError as exc:
        if staged is not None and not replaced:
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
        raise SaveError(path, exc, replaced=replaced) from exc
