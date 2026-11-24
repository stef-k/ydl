"""Values exchanged between the async workflow and its frontends."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path


class YdlError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Settings:
    scope: str
    quality: str
    keep_video: bool
    destination: Path


@dataclass(frozen=True)
class Decision:
    question: str
    choices: tuple[tuple[str, str], ...]
    default: str
    kind: str = ""
    video_id: str | None = None
    playlist_id: str | None = None


@dataclass(frozen=True)
class BackendStatus:
    state: str
    version: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class Event:
    stage: str
    message: str = ""
    settings: Settings | None = None
    item_id: str | None = None
    title: str | None = None
    position: int | None = None
    total: int | None = None
    percent: float | None = None
    speed: float | None = None
    eta: float | None = None
    backend: BackendStatus | None = None


@dataclass(frozen=True)
class Result:
    destination: Path | None = None
    saved: int = 0
    failed: int = 0
    diagnostics: tuple[str, ...] = ()
    declined: bool = False
    job_id: str | None = None
    log: Path | None = None
    attention: str = ""
    destination_changed: bool = False
    output_conflict: bool = False

    @property
    def code(self) -> int:
        return int(self.failed > 0)


Emit = Callable[[Event], None]
Decide = Callable[[Decision], Awaitable[str]]
