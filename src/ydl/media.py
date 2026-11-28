"""YouTube URL handling and machine-readable media operations."""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import mp3_tags
from .backend import backend_command
from .models import YdlError
from .process import run

PLAYLIST_LIMIT = 50
METADATA_TIMEOUT = 120
ID = re.compile(r"[A-Za-z0-9_-]{1,100}")
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
TRANSFER_METADATA = "ydl:metadata:"


@dataclass(frozen=True)
class Address:
    video: str | None
    playlist: str | None
    host: str

    def url(self, scope):
        if scope == "playlist":
            return f"https://{self.host}/playlist?list={self.playlist}"
        return f"https://{self.host}/watch?v={self.video}"

    def canonical(self):
        if not self.video:
            return self.url("playlist")
        suffix = f"&list={self.playlist}" if self.playlist else ""
        return self.url("single") + suffix


def parse_url(url):
    error = YdlError("Enter a YouTube watch, Shorts, youtu.be or playlist URL.", 2)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise error from exc
    host = parts.hostname
    if parts.scheme not in ("http", "https") or host not in HOSTS or parts.username is not None:
        raise error
    if port not in (None, 443 if parts.scheme == "https" else 80):
        raise error
    query = parse_qs(parts.query, keep_blank_values=True)
    if any(len(query.get(key, ())) > 1 for key in ("v", "list")):
        raise error
    playlist = query.get("list", [None])[0]
    video = video_id(parts.path, host, query)
    if not (video or playlist) or any(value is not None and not ID.fullmatch(value)
                                     for value in (video, playlist)):
        raise error
    return Address(video, playlist, "music.youtube.com" if host == "music.youtube.com" else "www.youtube.com")


def video_id(path, host, query):
    segments = path.strip("/").split("/")
    if host == "youtu.be" and len(segments) == 1 and segments[0]:
        return segments[0]
    if host != "youtu.be":
        if path == "/watch":
            return query.get("v", [""])[0]
        if len(segments) == 2 and segments[0] == "shorts":
            return segments[1]
        if path == "/playlist" and "list" in query:
            return None
    raise YdlError("Unsupported YouTube URL path.", 2)


def validate_scope(address, scope):
    if scope == "single" and not address.video:
        raise YdlError("--single requires a URL that names a video.", 2)
    if scope == "playlist" and not address.playlist:
        raise YdlError("--playlist requires a URL with playlist context.", 2)


def selected_command(backend, runtime):
    name, path, _ = runtime
    return [*backend_command(backend), "--no-js-runtimes", "--js-runtimes", f"{name}:{path}"]


def explanation(stderr, fallback):
    detail = stderr.lower()
    if "429" in detail or "too many requests" in detail:
        return "YouTube rate-limited the request. Wait and retry."
    if any(word in detail for word in ("sign in", "log in", "login required", "age-restricted")):
        return "YouTube requires authentication for this request."
    if any(word in detail for word in ("javascript runtime", "challenge solving failed", "n challenge")):
        return "YouTube challenge solving failed. Run ydl doctor and update the JavaScript runtime or yt-dlp."
    if "requested format is not available" in detail or "no video formats found" in detail:
        return "YouTube did not provide a usable requested format. Update yt-dlp or retry later."
    if any(word in detail for word in ("private video", "not available", "video unavailable", "removed")):
        return "This video or playlist is private, removed, or unavailable."
    if any(word in detail for word in ("timed out", "network", "unable to download", "connection", "http error")):
        return "The YouTube request failed over the network. Check connectivity and retry."
    tail = next((line.strip() for line in reversed(stderr.splitlines()) if line.strip()), "")
    return f"{fallback} {tail}".strip()


async def metadata(command, url, *, playlist, limit=None, log):
    flags = ["--yes-playlist", "--flat-playlist"] if playlist else ["--no-playlist"]
    if limit is not None:
        flags.extend(["--playlist-end", str(limit)])
    args = [*command, *flags, "--skip-download", "--dump-single-json", "--", url]
    try:
        output = await run(args, timeout=METADATA_TIMEOUT, log=log)
    except TimeoutError as exc:
        raise YdlError("YouTube metadata could not be read within 120 seconds.") from exc
    if output.code:
        raise YdlError(explanation(output.stderr, "Could not read YouTube metadata."))
    try:
        result = json.loads(output.stdout)
        if not isinstance(result, dict):
            raise ValueError("metadata is not an object")
    except ValueError as exc:
        raise YdlError("yt-dlp did not return valid YouTube metadata.") from exc
    return result


def playlist_size(metadata):
    total = metadata.get("playlist_count")
    total = total if type(total) is int and total >= 0 else None
    entries = metadata.get("entries")
    seen = len(entries) if isinstance(entries, list) else None
    if seen is not None and seen > PLAYLIST_LIMIT:
        return (max(total, seen), True) if total is not None else (None, True)
    if total is not None:
        return total, total > PLAYLIST_LIMIT
    if seen is not None and not metadata.get("is_incomplete"):
        return seen, False
    return None, None


def is_mix(playlist_id, info=None):
    if playlist_id.upper().startswith("RD"):
        return True
    reported_id = (info or {}).get("id")
    return isinstance(reported_id, str) and reported_id.upper().startswith("RD")


def entry_id(entry):
    value = entry.get("id")
    return value if isinstance(value, str) and ID.fullmatch(value) else None


def progress_record(line, log):
    marker, kind, payload = line.split(":", 2)
    if marker != "ydl" or kind not in ("path", "download", "postprocess"):
        return None
    try:
        return kind, json.loads(payload)
    except ValueError as exc:
        log.write(f"Invalid yt-dlp progress record: {exc}")
        return None


def reject_json_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def transfer_metadata(records, video_id):
    if len(records) != 1:
        return None
    try:
        record = json.loads(records[0], parse_constant=reject_json_constant)
    except (ValueError, RecursionError):
        return None
    return record if isinstance(record, dict) and record.get("id") == video_id else None


async def transfer(command, video_id, staging, keep_video, on_progress, log, *, collect_metadata=False):
    args = [*command, "--no-playlist", "--continue", "--no-overwrites",
            "-f", "bv*+ba/b" if keep_video else "bestaudio/best",
            "--progress", "--newline", "--progress-delta", "0.2",
            "--progress-template", "download:ydl:download:%(progress)j",
            "--progress-template", "postprocess:ydl:postprocess:%(progress)j",
            "--print", "after_move:ydl:path:%(filepath)j"]
    if collect_metadata:
        args.extend(["--print", f"after_move:{TRANSFER_METADATA}" "%(.{id,title,track,description,artists,album})j"])
    args.extend(["-o", str(staging / "source.%(ext)s"), "--", f"https://www.youtube.com/watch?v={video_id}"])
    sources = []
    records = []

    def on_line(line):
        if collect_metadata and line.startswith(TRANSFER_METADATA):
            records.append(line.removeprefix(TRANSFER_METADATA))
            return
        if not line.startswith("ydl:") or line.count(":") < 2:
            log.write(line)
            return
        record = progress_record(line, log)
        if record is None:
            return
        kind, value = record
        if kind == "path" and isinstance(value, str):
            sources.append(Path(value))
        elif isinstance(value, dict):
            on_progress(kind, value)

    result = await run(args, on_line=on_line, log=log)
    if result.code:
        raise YdlError(explanation(result.stderr, "yt-dlp could not download this item."))
    if not sources or sources[-1].parent.resolve() != staging.resolve():
        raise YdlError("yt-dlp did not report a source file in the job staging directory.")
    path = sources[-1]
    if path.is_symlink() or not path.is_file():
        raise YdlError("The staged source file is missing or invalid.")
    return path, transfer_metadata(records, video_id)


async def convert(ffmpeg, ffprobe, source, output, quality, candidates, log):
    tags = await source_tags(ffprobe, source, candidates, log)
    metadata = []
    for key in mp3_tags.MANAGED_TAGS:
        # An empty assignment removes an unusable tag copied from the source.
        metadata.extend(["-metadata", f"{key}={tags.get(key, '')}"])
    mode = "-b:a" if quality.endswith("K") else "-q:a"
    value = quality.lower() if mode == "-b:a" else quality
    result = await run([ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(source),
                        "-map", "0:a:0", "-c:a", "libmp3lame", mode, value, *metadata, str(output)], log=log)
    if result.code:
        raise YdlError("MP3 conversion failed. Check the job log for FFmpeg details.")


async def remux(ffmpeg, source, video, log):
    result = await run([ffmpeg, "-nostdin", "-v", "error", "-y", "-i", str(source),
                        "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", str(video)], log=log)
    if result.code:
        raise YdlError(f"Could not recreate the saved {video.suffix} container without re-encoding video.")


async def probe(ffprobe, path, log, error):
    result = await run([ffprobe, "-v", "error", "-show_format", "-show_streams",
                        "-of", "json", str(path)], timeout=30, log=log)
    if result.code:
        raise YdlError(error)
    try:
        info = json.loads(result.stdout)
        if not isinstance(info["format"], dict) or not isinstance(info["streams"], list):
            raise ValueError("invalid media probe result")
    except (ValueError, KeyError, TypeError) as exc:
        raise YdlError(error) from exc
    return info


async def source_tags(ffprobe, source, candidates, log):
    error = "FFprobe could not read tags from the staged source. Check the job log for details."
    info = await probe(ffprobe, source, log, error)
    try:
        audio = next(stream for stream in info["streams"] if stream.get("codec_type") == "audio")
        return mp3_tags.resolve(info["format"].get("tags", {}), audio.get("tags", {}), candidates)
    except (StopIteration, AttributeError) as exc:
        raise YdlError(error) from exc


async def verify_output(ffprobe, path, kind, log):
    error = f"FFprobe could not verify the staged {kind} output."
    info = await probe(ffprobe, path, log, error)
    streams = info["streams"]
    try:
        audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
        if kind == "mp3":
            valid = info["format"]["format_name"] == "mp3" and any(s.get("codec_name") == "mp3" for s in audio)
        else:
            valid = audio and any(s.get("codec_type") == "video" and
                                  not s.get("disposition", {}).get("attached_pic") for s in streams)
    except (ValueError, KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise YdlError(error)
