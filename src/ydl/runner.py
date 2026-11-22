"""The asynchronous download and retry entry points shared by frontends."""

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from . import backend, jobs, media
from .config import output_path
from .models import Decide, Decision, Emit, Event, Result, Settings, YdlError
from .process import Log


@dataclass(frozen=True)
class Download:
    url: str
    scope: str | None = None
    quality: str | None = None
    keep_video: bool | None = None
    output: str | None = None
    yes: bool = False
    no_update: bool = False


@dataclass(frozen=True)
class Dependencies:
    command: list[str]
    ffmpeg: str
    ffprobe: str


def yes_no(question, default="n"):
    return Decision(question, (("y", "Yes"), ("n", "No")), default)


async def scope_for(address, request, decide):
    media.validate_scope(address, request.scope)
    if request.scope:
        return request.scope
    if not address.video:
        return "playlist"
    if address.playlist and not request.yes and decide is not None:
        return await decide(Decision("Download", (("single", "This video"),
                                                   ("playlist", "Entire playlist")), "single"))
    return "single"


async def confirmation(request, decide, total, large):
    if request.yes:
        return True
    if decide is None:
        raise YdlError("Playlist size requires confirmation. Pass --yes to download the playlist.", 2)
    if total is not None:
        size = f"{total} entries"
    elif large:
        size = f"at least {media.PLAYLIST_LIMIT + 1} entries"
    else:
        size = "unknown size"
    return await decide(yes_no(f"Download the entire playlist ({size})?")) == "y"


def playlist_entries(info):
    entries = info.get("entries")
    if not isinstance(entries, list) or info.get("is_incomplete"):
        raise YdlError("YouTube did not return the complete playlist.")
    if not entries:
        raise YdlError("The playlist is empty or unavailable.")
    return entries


async def playlist_plan(address, request, deps, decide, log):
    if media.is_mix(address.playlist):
        raise YdlError("Auto-generated YouTube mixes are unsupported. Use --single for an individual video.")
    url = address.url("playlist")
    limit = media.PLAYLIST_LIMIT + 1
    probe = await media.metadata(deps.command, url, playlist=True, limit=limit, log=log)
    if media.is_mix(address.playlist, probe):
        raise YdlError("Auto-generated YouTube mixes are unsupported.")
    total, large = media.playlist_size(probe)
    confirmed = large is not False
    if confirmed and not await confirmation(request, decide, total, large):
        return None
    entries = probe.get("entries")
    complete = isinstance(entries, list) and len(entries) < limit and total == len(entries)
    info = probe if complete else await media.metadata(deps.command, url, playlist=True, log=log)
    entries = playlist_entries(info)
    if not confirmed and len(entries) > media.PLAYLIST_LIMIT:
        if not await confirmation(request, decide, len(entries), True):
            return None
    return info, entries


def planned_items(entries, playlist):
    items = []
    for position, entry in enumerate(entries, 1):
        entry = entry if isinstance(entry, dict) else {}
        video = media.entry_id(entry)
        title = entry.get("title")
        if not isinstance(title, str) or not title:
            title = video or "Unavailable entry"
        items.append(jobs.make_item(position, video, title, playlist))
    return items


def add_playlist_directory(destination, name, playlist_id):
    text = name if isinstance(name, str) else ""
    return destination / f"{jobs.safe_name(text, 'Playlist')} [{playlist_id}]"


async def get_plan(address, scope, request, deps, decide, log):
    if scope == "playlist":
        return await playlist_plan(address, request, deps, decide, log)
    info = await media.metadata(deps.command, address.url("single"), playlist=False, log=log)
    return info, [{**info, "id": address.video}]


async def keep_video_for(request, prefs, decide):
    if request.keep_video is not None:
        return request.keep_video
    if request.yes or decide is None:
        return prefs.keep_video
    return await decide(yes_no("Keep original video file(s)?", "y" if prefs.keep_video else "n")) == "y"


async def prepare(paths, prefs, decide, emit, log, *, no_update=False):
    ffmpeg = backend.require_ffmpeg()
    await backend.ensure_backend(paths.backend, prefs, no_update=no_update, emit=emit, log=log)
    runtime = await backend.require_runtime(paths.data, decide, emit, log)
    return Dependencies(media.selected_command(paths.backend, runtime), ffmpeg["ffmpeg"], ffmpeg["ffprobe"])


async def download(request: Download, paths, prefs, *, emit: Emit, decide: Decide | None = None):
    """Plan and execute a new download, or return after confirmation is declined."""
    address = media.parse_url(request.url)
    scope = await scope_for(address, request, decide)
    if scope == "playlist" and media.is_mix(address.playlist):
        raise YdlError("Auto-generated YouTube mixes are unsupported. Use --single for an individual video.")
    target = output_path(request.output if request.output is not None else prefs.output)
    quality = request.quality if request.quality is not None else prefs.quality
    job_id = uuid4().hex
    log = Log(paths.state / "logs" / f"{job_id}.log")
    deps = await prepare(paths, prefs, decide, emit, log, no_update=request.no_update)
    emit(Event("metadata", "Fetching YouTube metadata"))
    plan = await get_plan(address, scope, request, deps, decide, log)
    if plan is None:
        return Result(declined=True)
    info, entries = plan
    keep_video = await keep_video_for(request, prefs, decide)
    destination = create_destination(target, scope, info, address)
    settings = Settings(scope, quality, keep_video, destination)
    job = jobs.Job.create(settings, planned_items(entries, scope == "playlist"))
    job.id = job_id
    store = jobs.JobStore(paths.state)
    store.save(job)
    with store.claim(job.id):
        return await execute(job, store, paths, prefs, emit, decide, log, deps)


def create_destination(target, scope, info, address):
    target.mkdir(parents=True, exist_ok=True)
    if scope == "single":
        return target
    generated = add_playlist_directory(target, info.get("title"), address.playlist)
    return jobs.local_directory(generated)


async def retry(job_id, paths, prefs, *, emit: Emit, decide: Decide | None = None):
    """Resume an existing plan using only the saved item IDs and destination."""
    store = jobs.JobStore(paths.state)
    selected = store.select(job_id)
    with store.claim(selected.id):
        job = store.load(selected.id)
        log = Log(store.logs / f"{job.id}.log")
        destination = Path(job.destination)
        if job.scope == "playlist":
            destination.parent.mkdir(parents=True, exist_ok=True)
            jobs.local_directory(destination)
        else:
            destination.mkdir(parents=True, exist_ok=True)
        return await execute(job, store, paths, prefs, emit, decide, log)


def item_event(job, item, stage, **metrics):
    return Event(stage, item_id=item.video_id, title=item.title,
                 position=item.position if job.scope == "playlist" else None,
                 total=len(job.items) if job.scope == "playlist" else None, **metrics)


def progress(job, item, emit, kind, data):
    if kind == "postprocess" or data.get("status") == "finished":
        emit(item_event(job, item, "converting"))
        return
    current = numeric(data.get("downloaded_bytes"))
    total = numeric(data.get("total_bytes"))
    percent = min(100, 100 * current / total) if current is not None and total else None
    emit(item_event(job, item, "downloading", percent=percent,
                    speed=numeric(data.get("speed")), eta=numeric(data.get("eta"))))


def numeric(value):
    return value if type(value) in (int, float) and 0 <= value < float("inf") else None


async def staged_source(job, item, deps, emit, store, staging, log):
    if source := jobs.find_source(staging, item):
        return source
    emit(item_event(job, item, "downloading"))
    url = f"https://www.youtube.com/watch?v={item.video_id}"
    callback = lambda kind, data: progress(job, item, emit, kind, data)
    source = await media.transfer(deps.command, url, staging, job.keep_video, callback, log)
    item.source = source.name
    store.save(job)
    return source


async def staged_outputs(job, pending, source, deps, staging, item, log):
    outputs = {}
    if "mp3" in pending:
        mp3 = staging / "verified.mp3"
        await media.convert(deps.ffmpeg, source, mp3, job.quality, log)
        await media.verify_output(deps.ffprobe, mp3, "mp3", log)
        outputs["mp3"] = mp3, pending["mp3"]
    if "video" in pending:
        suffix = jobs.video_suffix(source)
        target = pending["video"] or (Path(job.destination) / f"{item.stem}{suffix}")
        video = source
        if target.suffix != suffix:
            video = staging / f"remuxed{target.suffix}"
            await media.remux(deps.ffmpeg, source, video, log)
        await media.verify_output(deps.ffprobe, video, "video", log)
        outputs["video"] = video, target
    return outputs


async def process_item(job, item, store, deps, emit, log):
    if not item.video_id:
        raise YdlError("The playlist entry has no usable video ID.")
    pending = await jobs.pending_outputs(job, item)
    if not pending:
        return
    staging = jobs.staging_path(job, item)
    source = await staged_source(job, item, deps, emit, store, staging, log)
    emit(item_event(job, item, "converting"))
    outputs = await staged_outputs(job, pending, source, deps, staging, item, log)
    await jobs.publish(store, job, item, outputs)
    try:
        jobs.remove_staging(staging)
    except OSError as exc:
        log.write(f"Could not remove staging files after publication: {exc}")


async def requires_media(job):
    for item in job.items:
        if not item.video_id:
            continue
        try:
            if await jobs.pending_outputs(job, item):
                return True
        except (OSError, YdlError):
            # An occupied or inaccessible output is reported per item.
            continue
    return False


async def execute(job, store, paths, prefs, emit, decide, log, deps=None):
    emit(Event("job", job.id))
    if deps is None and await requires_media(job):
        deps = await prepare(paths, prefs, decide, emit, log)
    emit(Event("settings", settings=job.settings))
    diagnostics = []
    saved = 0
    for item in job.items:
        try:
            await process_item(job, item, store, deps, emit, log)
            saved += 1
            emit(item_event(job, item, "saved"))
        except (OSError, TimeoutError, YdlError) as exc:
            detail = str(exc) or "Media verification timed out."
            message = f"{item.title}: {detail}"
            log.write(message)
            diagnostics.append(message)
            emit(item_event(job, item, "failed", message=detail))
    job.complete = not diagnostics
    store.save(job)
    return Result(Path(job.destination), saved, len(diagnostics), tuple(diagnostics),
                  job_id=job.id, log=log.path)
