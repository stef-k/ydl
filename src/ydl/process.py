"""Cancellable POSIX subprocesses with independently drained output streams."""

import asyncio
import os
import signal
from collections import deque
from dataclasses import dataclass
from pathlib import Path


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
            callback(line.decode(errors="replace").rstrip("\r"))
    if buffer:
        callback(buffer.decode(errors="replace"))


def signal_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


async def stop(process, pending):
    signal_group(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(asyncio.shield(pending), 2)
    except TimeoutError:
        signal_group(process.pid, signal.SIGKILL)
        await pending
    finally:
        signal_group(process.pid, signal.SIGKILL)


async def run(args, *, timeout=None, log=None, on_line=None):
    async with asyncio.timeout(timeout):
        return await run_child(args, log, on_line)


async def run_child(args, log, on_line):
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )
    tail = deque(maxlen=40)

    def diagnostic(line):
        tail.append(line)
        if log is not None:
            log.write(line)

    pending = asyncio.gather(
        process.wait(),
        process.stdout.read() if on_line is None else read_lines(process.stdout, on_line),
        read_lines(process.stderr, diagnostic),
    )
    try:
        code, stdout, _ = await asyncio.shield(pending)
    except BaseException:
        try:
            await stop(process, pending)
        except Exception:
            # Preserve the original failure, including cancellation.
            pass
        raise
    return ProcessResult(code, stdout or b"", "\n".join(tail))
