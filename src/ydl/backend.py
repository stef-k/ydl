"""Installation, release checks and media dependencies."""

import asyncio
import fcntl
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sys
import tempfile
import zipfile
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import atomic_write
from .models import BackendStatus, Decide, Decision, Emit, Event, YdlError
from .process import Log, run

REPOSITORIES = {"stable": "yt-dlp/yt-dlp", "nightly": "yt-dlp/yt-dlp-nightly-builds"}
VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+")
DENO_REPOSITORY = "denoland/deno"
UPDATE_TIMEOUT = 60


@dataclass(frozen=True)
class Release:
    repository: str
    tag: str

    def asset(self, name):
        return f"https://github.com/{self.repository}/releases/download/{self.tag}/{name}"


async def fetch(url, limit, log):
    response = await run([sys.executable, "-m", "ydl._fetch", url, str(limit)], log=log)
    if response.code:
        raise YdlError(f"Could not download release information or asset: {response.stderr or url}")
    return response.stdout


async def latest(repository, log):
    url = f"https://api.github.com/repos/{repository}/releases/latest"
    raw = await fetch(url, 2 * 1024 * 1024, log)
    try:
        tag = json.loads(raw)["tag_name"]
        if not isinstance(tag, str) or not VERSION.fullmatch(tag.removeprefix("v")):
            raise ValueError("unexpected release tag")
    except (ValueError, KeyError, TypeError) as exc:
        raise YdlError(f"Invalid release metadata for {repository}.") from exc
    return Release(repository, tag)


def checksum(manifest, asset, *, single=False):
    for line in manifest.decode(errors="replace").splitlines():
        fields = line.split()
        if not fields or not re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]):
            continue
        if len(fields) == 1 and single:
            return fields[0].lower()
        if len(fields) == 2 and fields[1].lstrip("*") == asset:
            return fields[0].lower()
    raise YdlError(f"Release does not provide a valid SHA-256 for {asset}.")


def verified(data, expected):
    if hashlib.sha256(data).hexdigest() != expected:
        raise YdlError("Release asset SHA-256 does not match the published checksum.")
    return data


def backend_command(path):
    return [sys.executable, str(path), "--ignore-config", "--no-plugin-dirs", "--no-update"]


async def backend_version(path, log=None):
    if path.is_symlink() or not path.is_file() or not os.access(path, os.X_OK):
        return None
    try:
        result = await run([*backend_command(path), "--version"], timeout=8, log=log)
    except (OSError, TimeoutError):
        return None
    value = result.stdout.decode(errors="replace").strip()
    return value if result.code == 0 and VERSION.fullmatch(value) else None


async def install_backend(path, payload, tag, installed, log):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".update-", dir=path.parent) as directory:
        staged = Path(directory) / "yt-dlp"
        atomic_write(staged, payload, 0o700)
        if await backend_version(staged, log) != tag:
            raise YdlError("The verified yt-dlp asset does not report the selected release version.")
        if installed:
            atomic_write(path.with_name("yt-dlp.previous"), path.read_bytes(), 0o700)
        staged.replace(path)


async def compare_backend(path, channel, *, installed, check, emit, log):
    release = await latest(REPOSITORIES[channel], log)
    manifest = await fetch(release.asset("SHA2-256SUMS"), 512 * 1024, log)
    digest = checksum(manifest, "yt-dlp")
    current = False
    if installed:
        with path.open("rb") as stream:
            current = hashlib.file_digest(stream, "sha256").hexdigest() == digest
    state = "up to date" if current else "replacement available" if installed else "installation available"
    local = installed or ("unusable" if path.exists() else "missing")
    report = f"Installed: {local} | {channel}: {release.tag} | {state}"
    if check:
        return report
    if current:
        backend_status(emit, "Up to date", installed)
        return report
    emit(Event("updating", f"Installing yt-dlp {release.tag}"))
    backend_status(emit, "Updating", installed)
    payload = verified(await fetch(release.asset("yt-dlp"), 32 * 1024 * 1024, log), digest)
    await install_backend(path, payload, release.tag, installed, log)
    backend_status(emit, "Up to date", release.tag)
    return f"Installed yt-dlp {release.tag} ({channel})."


@asynccontextmanager
async def installation_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(".update.lock").open("a") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.1)
        yield


def backend_status(emit, state, version=None, detail=""):
    emit(Event("backend", backend=BackendStatus(state, version, detail)))


async def ensure_backend(path, prefs, *, no_update=False, explicit=False, check=False,
                         emit: Emit, log: Log):
    emit(Event("checking", "Checking yt-dlp"))
    backend_status(emit, "Checking")
    installed = await backend_version(path, log)
    if no_update:
        if not installed:
            raise YdlError("The private yt-dlp backend is missing or unusable. Run ydl update.")
        return None
    if installed and not (prefs.auto_update or explicit):
        backend_status(emit, "Ready · updates off", installed)
        return None
    try:
        async with asyncio.timeout(UPDATE_TIMEOUT):
            if check:
                return await compare_backend(path, prefs.channel, installed=installed, check=True,
                                             emit=emit, log=log)
            async with installation_lock(path):
                installed = await backend_version(path, log)
                if installed and not (prefs.auto_update or explicit):
                    backend_status(emit, "Ready · updates off", installed)
                    return None
                return await compare_backend(path, prefs.channel, installed=installed, check=False,
                                             emit=emit, log=log)
    except (TimeoutError, OSError, YdlError) as exc:
        detail = str(exc) or "release check timed out"
        log.write(f"Backend update failed: {detail}")
        if not explicit and (installed := await backend_version(path, log)):
            emit(Event("warning", f"yt-dlp update failed. Using {installed}. {detail}"))
            backend_status(emit, "Ready · using installed version", installed, detail)
            return None
        backend_status(emit, "Unavailable", installed, detail)
        raise YdlError(f"yt-dlp update failed: {detail}") from exc


def require_ffmpeg():
    tools = {name: shutil.which(name) for name in ("ffmpeg", "ffprobe")}
    missing = [name for name, path in tools.items() if not path]
    if missing:
        raise YdlError(f"Missing {', '.join(missing)}. Install the Linux ffmpeg package and retry.")
    return tools


async def runtime_version(path, minimum):
    try:
        result = await run([path, "--version"], timeout=5)
    except (OSError, TimeoutError):
        return None
    first_line = result.stdout.decode(errors="replace").partition("\n")[0]
    match = re.search(r"\b(?:v)?(\d+)\.(\d+)\.(\d+)", first_line)
    if result.code or not match:
        return None
    version = tuple(map(int, match.groups()))
    return match[0].removeprefix("v") if version >= minimum else None


async def detect_runtime(data):
    deno = shutil.which("deno")
    node = shutil.which("node")
    private = data / "bin" / "deno"
    candidates = [("deno", deno, (2, 3, 0)), ("node", node, (22, 0, 0)),
                  ("deno", str(private), (2, 3, 0))]
    for name, path, minimum in candidates:
        if path and (version := await runtime_version(path, minimum)):
            return name, path, version
    return None


async def install_deno(data, log):
    arch = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(platform.machine())
    if not arch:
        raise YdlError("Automatic Deno installation supports x86_64 and aarch64. Install Deno or Node manually.")
    asset = f"deno-{arch}-unknown-linux-gnu.zip"
    release = await latest(DENO_REPOSITORY, log)
    manifest = await fetch(release.asset(f"{asset}.sha256sum"), 8192, log)
    digest = checksum(manifest, asset, single=True)
    archive_data = verified(await fetch(release.asset(asset), 128 * 1024 * 1024, log), digest)
    with zipfile.ZipFile(io.BytesIO(archive_data)) as archive:
        binary = archive.read("deno")
    path = data / "bin" / "deno"
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".deno-", dir=path.parent) as directory:
        staged = Path(directory) / "deno"
        atomic_write(staged, binary, 0o700)
        version = await runtime_version(str(staged), (2, 3, 0))
        if version is None:
            raise YdlError("The verified Deno executable failed its compatibility check.")
        staged.replace(path)
    return "deno", str(path), version


async def require_runtime(data, decide: Decide | None, emit: Emit, log: Log):
    emit(Event("checking", "Checking the YouTube JavaScript runtime"))
    if runtime := await detect_runtime(data):
        return runtime
    message = "YouTube requires Deno 2.3+ or Node 22+. Install one on PATH or approve the private Deno installation."
    request = Decision("Install Deno in ydl’s user-data directory?", (("y", "Yes"), ("n", "No")), "n")
    if decide is None or await decide(request) != "y":
        raise YdlError(message)
    emit(Event("updating", "Installing Deno"))
    try:
        async with asyncio.timeout(UPDATE_TIMEOUT):
            return await install_deno(data, log)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile, TimeoutError) as exc:
        raise YdlError(f"Deno installation failed: {str(exc) or 'timed out'}. {message}") from exc
