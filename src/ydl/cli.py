"""Command-line entry point for ydl."""

import argparse
import asyncio
import os
import shutil
import sys

from . import backend, config, runner
from .models import YdlError
from .process import Log, run
from .terminal import Terminal, clean


def parser_for(command):
    parser = argparse.ArgumentParser(prog="ydl" if command == "download" else f"ydl {command}")
    if command == "download":
        parser.description = "Download a YouTube video or playlist as MP3."
        parser.epilog = "Other commands: tui [URL], retry [JOB_ID], doctor, config show, config set, update [--check]."
        parser.add_argument("url", metavar="URL")
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument("--single", action="store_const", dest="scope", const="single")
        scope.add_argument("--playlist", action="store_const", dest="scope", const="playlist")
        retain = parser.add_mutually_exclusive_group()
        retain.add_argument("--keep-video", action="store_true", default=None)
        retain.add_argument("--no-keep-video", action="store_false", dest="keep_video")
        parser.add_argument("--quality", metavar="QUALITY")
        parser.add_argument("--output", metavar="DIRECTORY")
        parser.add_argument("--yes", action="store_true")
        parser.add_argument("--no-update", action="store_true")
    elif command == "retry":
        parser.add_argument("job_id", nargs="?", metavar="JOB_ID")
    elif command == "tui":
        parser.description = "Open the interactive download queue. An optional URL pre-fills the draft."
        parser.add_argument("url", nargs="?", metavar="URL")
    elif command == "update":
        parser.add_argument("--check", action="store_true")
    elif command == "config":
        sub = parser.add_subparsers(dest="action", required=True)
        sub.add_parser("show")
        setter = sub.add_parser("set")
        setter.add_argument("key", choices=("output", "quality", "keep_video", "channel"))
        setter.add_argument("value")
    return parser


def arguments(argv):
    commands = {"config", "doctor", "retry", "tui", "update"}
    command = argv[0] if argv and argv[0] in commands else "download"
    return command, parser_for(command).parse_args(argv if command == "download" else argv[1:])


def file_status(folder):
    candidate = folder
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate.is_dir() and os.access(candidate, os.W_OK | os.X_OK)


async def tool_status(name):
    path = shutil.which(name)
    if path:
        try:
            result = await run([path, "-version"], timeout=8)
            if result.code == 0 and result.stdout:
                return clean(result.stdout.decode(errors="replace").splitlines()[0])
        except (TimeoutError, OSError):
            pass
    return None


async def doctor(paths, prefs):
    ok = True
    version = await backend.backend_version(paths.backend)
    print(f"yt-dlp: {version}" if version else "yt-dlp: missing or unusable. Run ydl update.")
    ok &= bool(version)
    for name in ("ffmpeg", "ffprobe"):
        detail = await tool_status(name)
        print(detail or f"{name}: missing or unusable. Install the Linux ffmpeg package.")
        ok &= bool(detail)
    runtime = await backend.detect_runtime(paths.data)
    if runtime:
        print(f"JavaScript: {runtime[0]} {runtime[2]} ({clean(runtime[1])})")
    else:
        print("JavaScript: install Deno 2.3+ or Node 22+ on PATH, or accept the Deno prompt.")
    ok &= bool(runtime)
    for name, path in {"config": paths.config.parent, "data": paths.data, "state": paths.state,
                       "output": config.output_path(prefs.output)}.items():
        writable = file_status(path)
        detail = "writable" if writable else "choose a writable directory or fix its permissions"
        print(f"{name}: {clean(path)} ({detail})")
        ok &= writable
    print(f"Configuration: valid ({prefs.channel}, automatic updates {'on' if prefs.auto_update else 'off'})")
    return 0 if ok else 1


async def dispatch(command, args, paths, prefs, terminal):
    decide = terminal.decide if sys.stdin.isatty() else None
    if command == "download":
        result = await runner.download(runner.Download(**vars(args)), paths, prefs,
                                       emit=terminal.emit, decide=decide)
    elif command == "retry":
        result = await runner.retry(args.job_id, paths, prefs, emit=terminal.emit, decide=decide)
    elif command == "update":
        log = Log(paths.state / "logs" / "update.log")
        report = await backend.ensure_backend(paths.backend, prefs, explicit=True, check=args.check,
                                              emit=terminal.emit, log=log)
        terminal.finish(report)
        return 0
    elif command == "config":
        if args.action == "set":
            prefs = config.set_value(paths.config, prefs, args.key, args.value)
        print(config.toml_text(prefs), end="")
        return 0
    else:
        return await doctor(paths, prefs)
    terminal.complete(result)
    return result.code


async def run_command(command, args, paths, preferences, terminal):
    if not terminal.tty or command in ("config", "doctor"):
        return await dispatch(command, args, paths, preferences, terminal)
    animation = asyncio.create_task(terminal.animate())
    try:
        return await dispatch(command, args, paths, preferences, terminal)
    finally:
        animation.cancel()
        await asyncio.gather(animation, return_exceptions=True)


def launch_tui(url):
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()) or os.environ.get("TERM") in (None, "", "dumb"):
            raise YdlError("ydl tui needs an interactive terminal. For non-interactive use, run ydl URL.")
        try:
            from .tui import launch
        except ModuleNotFoundError as exc:
            if exc.name != "textual":
                raise
            raise YdlError("Textual is required for ydl tui. From the checkout run pipx install '.[tui]'.") from exc
        return launch(config.Paths.discover(), url)
    except (YdlError, OSError) as exc:
        print(clean(str(exc)), file=sys.stderr)
        return 1


def main(argv=None):
    command, args = arguments(sys.argv[1:] if argv is None else argv)
    if command == "tui":
        return launch_tui(args.url)
    terminal = Terminal()
    try:
        paths = config.Paths.discover()
        preferences = config.load(paths.config)
        return asyncio.run(run_command(command, args, paths, preferences, terminal))
    except KeyboardInterrupt as exc:
        terminal.fail(exc)
        return 130
    except (YdlError, OSError) as exc:
        terminal.fail(exc)
        return getattr(exc, "code", 1)
