"""Cancellable POSIX subprocesses with independently drained output streams."""

import asyncio
import os
import sys
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path


_ownership = ContextVar("process_ownership", default=())


@contextmanager
def inherit_lock(fd):
    token = _ownership.set((*_ownership.get(), fd))
    try:
        yield
    finally:
        _ownership.reset(token)


@dataclass(frozen=True)
class ProcessResult:
    code: int
    stdout: bytes
    stderr: str


class Log:
    def __init__(self, path: Path | None = None):
        self.path = path

    def write(self, message):
        if self.path is not None:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(message.rstrip("\n") + "\n")


async def read_lines(reader, callback):
    buffer = b""
    while chunk := await reader.read(65536):
        lines = (buffer + chunk).split(b"\n")
        buffer = lines.pop()
        for line in lines:
            callback(line.rstrip(b"\r"))
    if buffer:
        callback(buffer)


async def run(args, *, timeout=None, log=None, on_line=None):
    """Run a child, streaming stdout lines as bytes when a callback is supplied."""
    async with asyncio.timeout(timeout):
        return await run_child(args, log, on_line)


async def finish_cleanup(cleanup):
    # A timeout and a user stop can cancel the same task in succession.
    # Keep the original outcome and leave cleanup running until it settles.
    work = asyncio.gather(cleanup, return_exceptions=True)
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            continue


async def drain_spawn(spawn):
    process = await spawn
    await process.communicate()


async def run_child(args, log, on_line):
    read_end, write_end = os.pipe()
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
        sys.executable, "-m", "ydl._supervise", str(read_end), *args,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
        pass_fds=(read_end, *_ownership.get()),
    ))
    try:
        process = await asyncio.shield(spawn)
    except asyncio.CancelledError:
        os.close(write_end)
        await finish_cleanup(drain_spawn(spawn))
        raise
    except BaseException:
        os.close(write_end)
        raise
    finally:
        os.close(read_end)
    return await collect(process, log, on_line, write_end)


async def drain_after_stop(process, readers, pending):
    try:
        results = await asyncio.gather(*readers, return_exceptions=True)
        if any(isinstance(result, BaseException) for result in results):
            await process.communicate()
    finally:
        await asyncio.gather(pending, return_exceptions=True)
        await process.wait()


async def collect(process, log, on_line, lifetime):
    tail = deque(maxlen=40)

    def diagnostic(raw):
        line = raw.decode(errors="replace")
        tail.append(line)
        if log is not None:
            log.write(line)

    stdout, stderr = map(asyncio.create_task, (
        process.stdout.read() if on_line is None else read_lines(process.stdout, on_line),
        read_lines(process.stderr, diagnostic),
    ))
    pending = asyncio.gather(process.wait(), stdout, stderr)
    try:
        code, stdout, _ = await asyncio.shield(pending)
    except BaseException:
        # Closing the lifetime pipe requests cleanup even if the tree forked.
        os.close(lifetime)
        await finish_cleanup(drain_after_stop(process, (stdout, stderr), pending))
        raise
    os.close(lifetime)
    return ProcessResult(code, stdout or b"", "\n".join(tail))
