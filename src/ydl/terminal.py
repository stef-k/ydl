"""Terminal rendering and cancellable interactive decisions."""

import asyncio
import os
import sys
import unicodedata
from pathlib import Path

from .models import YdlError

CLEAR = "\r\x1b[2K"
SPINNER = "|/-\\"
ACTIVE = {"checking", "updating", "metadata", "downloading", "converting"}
STAGES = {"downloading": "Downloading", "converting": "Converting", "saved": "Saved", "failed": "Failed"}


def clean(text):
    return "".join(" " if unicodedata.category(char).startswith("C") else char for char in str(text))


def width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def clip(text, limit):
    if sum(map(width, text)) <= limit:
        return text
    result = []
    remaining = max(0, limit - 1)
    for char in text:
        remaining -= width(char)
        if remaining < 0:
            break
        result.append(char)
    return "".join(result) + ("…" if limit > 0 else "")


def display_path(path):
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)


def settings_line(settings):
    scope = "Playlist" if settings.scope == "playlist" else "Single video"
    quality = settings.quality if settings.quality.endswith("K") else f"VBR {settings.quality}"
    retention = "on" if settings.keep_video else "off"
    return f"{scope} | MP3 {quality} | Keep video {retention} | {display_path(settings.destination)}"


def metrics(event):
    values = []
    if event.percent is not None:
        values.append(f"{event.percent:.0f}%")
    if event.speed is not None:
        divisor, unit = (1048576, "MiB") if event.speed >= 1048576 else (1024, "KiB")
        values.append(f"{event.speed / divisor:.1f} {unit}/s")
    if event.eta is not None:
        minutes, seconds = divmod(int(event.eta), 60)
        values.append(f"ETA {minutes:02d}:{seconds:02d}")
    return "".join(f" | {value}" for value in values)


def render_event(event, column_limit, spinner):
    label = STAGES.get(event.stage)
    animation = f" {spinner}" if event.stage in ACTIVE and event.percent is None else ""
    if label is None:
        return clip(clean(event.message) + animation, column_limit)
    prefix = f"[{event.position}/{event.total}] " if event.position is not None else ""
    prefix += label + " "
    suffix = metrics(event) + animation
    title = clip(clean(event.title or ""), max(0, column_limit - len(prefix) - len(suffix)))
    return clip(prefix + title + suffix, column_limit)


async def read_terminal_line():
    event_loop = asyncio.get_running_loop()
    result = event_loop.create_future()
    file = sys.stdin.fileno()

    def available():
        event_loop.remove_reader(file)
        try:
            result.set_result(sys.stdin.readline())
        except OSError as exc:
            result.set_exception(exc)

    event_loop.add_reader(file, available)
    try:
        return await result
    finally:
        event_loop.remove_reader(file)


class Terminal:
    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stderr
        self.tty = self.stream.isatty() and os.environ.get("TERM") not in (None, "", "dumb")
        self.visible = False
        self.settings = False
        self.current = None
        self.frame = 0
        self.job_id = None
        self.destination = None
        self.log = None

    def write(self, text):
        print(text, end="", file=self.stream, flush=True)

    def columns(self):
        try:
            return max(1, os.get_terminal_size(self.stream.fileno()).columns - 1)
        except OSError:
            return 79

    def clear(self):
        if self.visible:
            self.write(CLEAR)
        self.visible = False

    def draw(self):
        if self.tty and self.current is not None:
            spinner = SPINNER[self.frame % len(SPINNER)]
            self.write(CLEAR + render_event(self.current, self.columns(), spinner))
            self.visible = True

    def emit(self, event):
        if event.stage == "job":
            self.job_id = event.message
        elif event.stage == "settings":
            self.show_settings(event.settings)
        elif event.stage == "warning":
            self.current = None
            self.clear()
            self.write(f"Warning: {clean(event.message)}\n")
        else:
            self.current = event
            self.draw()

    def show_settings(self, settings):
        self.current = None
        self.clear()
        self.destination = settings.destination
        if self.tty:
            self.settings = True
            self.write(clip(clean(settings_line(settings)), self.columns()) + "\n")

    async def animate(self):
        while True:
            await asyncio.sleep(0.15)
            current = self.current
            if current and current.stage in ACTIVE and current.percent is None:
                self.frame += 1
                self.draw()

    async def decide(self, decision):
        self.current = None
        self.clear()
        keys = dict(decision.choices)
        is_yes_no = set(keys) == {"y", "n"}
        if is_yes_no:
            choices = "[Y/n]" if decision.default == "y" else "[y/N]"
            prompt = f"{decision.question} {choices}: "
            aliases = {"yes": "y", "no": "n"}
        else:
            aliases = {str(index): value for index, value in enumerate(keys, 1)}
            parts = [f"{index}) {keys[value]}" + (" [default]" if value == decision.default else "")
                     for index, value in aliases.items()]
            prompt = f"{decision.question}: {'  '.join(parts)}: "
        while True:
            self.write(clean(prompt))
            raw = await read_terminal_line()
            if not raw:
                self.write("\n")
            value = raw.strip().lower()
            selection = aliases.get(value, value) if value else decision.default
            if selection in keys:
                return selection
            self.write("Choose one of the listed options.\n")

    def finish(self, message):
        self.current = None
        self.clear()
        self.write(clean(message) + "\n")

    def complete(self, result):
        if result.declined:
            self.finish("No downloads started.")
            return
        summary = f"Completed: {result.saved} MP3 files, {result.failed} failed"
        if result.destination is not None and not self.settings:
            summary += f" | {display_path(result.destination)}"
        self.finish(summary)
        for detail in dict.fromkeys(result.diagnostics[:8]):
            self.write(clean(detail) + "\n")
        if result.failed:
            self.write(f"Retry: ydl retry {result.job_id}\nLog: {clean(result.log)}\n")

    def fail(self, error):
        destination = f" | {display_path(self.destination)}" if self.destination and not self.settings else ""
        if isinstance(error, KeyboardInterrupt):
            hint = f". Resume with ydl retry {self.job_id}" if self.job_id else "."
            self.finish(f"Cancelled{hint}{destination}")
        elif isinstance(error, YdlError):
            self.finish(f"Error: {error}{destination}")
        else:
            self.finish(f"Error: Could not access the requested files. {error}{destination}")
