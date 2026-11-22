"""The asynchronous download and retry entry points shared by frontends."""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from . import backend, jobs, media
from .config import SaveError, output_path, quality_value
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


class DecisionDismissed(Exception):
    """The frontend dismissed this decision without selecting an answer."""


class AttemptStopped(asyncio.CancelledError):
    def __init__(self, *, planned):
        self.planned = planned


class AttemptDismissed(YdlError):
    def __init__(self, *, planned):
        super().__init__("Download prompt dismissed.")
        self.planned = planned


class UncertainPlan(jobs.PlanNeedsSync):
    pass


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
                                                   ("playlist", "Entire playlist")), "single",
                                     "scope", address.video, address.playlist))
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
    expected = info.get("playlist_count")
    if type(expected) is int and expected > len(entries):
        raise YdlError("YouTube listed fewer playlist entries than reported. Retry when the full list is available.")
    return entries


async def playlist_plan(address, request, deps, decide, log):
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


async def prepare(paths, prefs, decide, emit, log, *, no_update=False, backend_ready=False):
    ffmpeg = backend.require_ffmpeg()
    if not backend_ready:
        await backend.ensure_backend(paths.backend, prefs, no_update=no_update, emit=emit, log=log)
    runtime = await backend.require_runtime(paths.data, decide, emit, log)
    return Dependencies(media.selected_command(paths.backend, runtime), ffmpeg["ffmpeg"], ffmpeg["ffprobe"])


async def download(request: Download, paths, prefs, *, emit: Emit, decide: Decide | None = None,
                   job_id=None, backend_ready=False):
    """Plan and execute a new download, or return after confirmation is declined."""
    reserved = job_id is not None
    job_id = job_id if reserved else uuid4().hex
    store = jobs.JobStore(paths.state)
    with store.claim(job_id, new=True):
        if os.path.lexists(store.path(job_id)):
            raise jobs.OccupiedPlan(f"Job ID already has a saved plan: {store.path(job_id)}")
        try:
            prepared = await build_job(request, paths, prefs, job_id, reserved, emit, decide, backend_ready)
        except asyncio.CancelledError:
            if not reserved:
                raise
            raise AttemptStopped(planned=False) from None
        except DecisionDismissed:
            raise AttemptDismissed(planned=False) from None
        if prepared is None:
            return Result(declined=True)
        job, deps, log, guard = prepared
        try:
            store.save(job)
        except SaveError as exc:
            if reserved and exc.replaced:
                raise UncertainPlan(f"Could not sync the new job plan at {store.path(job_id)}: {exc}") from exc
            raise
        try:
            return await execute(job, store, paths, prefs, emit, decide, log, deps, guard=guard)
        except asyncio.CancelledError:
            if not reserved:
                raise
            raise AttemptStopped(planned=True) from None


async def build_job(request, paths, prefs, job_id, reserved, emit, decide, backend_ready):
    address = media.parse_url(request.url)
    destination_option = request.output if request.output is not None else prefs.output
    if reserved:
        target = Path(destination_option)
        base_identity = jobs.safe_destination(target)
    scope = await scope_for(address, request, decide)
    if scope == "playlist" and media.is_mix(address.playlist):
        raise YdlError("Auto-generated YouTube mixes are unsupported. Use --single for an individual video.")
    if not reserved:
        target = output_path(destination_option)
    quality = quality_value(request.quality if request.quality is not None else prefs.quality)
    log = Log(paths.state / "logs" / f"{job_id}.log")
    deps = await prepare(paths, prefs, decide, emit, log, no_update=request.no_update, backend_ready=backend_ready)
    emit(Event("metadata", "Fetching YouTube metadata"))
    plan = await get_plan(address, scope, request, deps, decide, log)
    if plan is None:
        return None
    info, entries = plan
    keep_video = await keep_video_for(request, prefs, decide)
    if reserved and jobs.safe_destination(target) != base_identity:
        raise jobs.DestinationChanged(f"Destination changed during planning: {target}. Restore the folder and Recheck.")
    destination = create_destination(target, scope, info, address, queue=reserved)
    guard = jobs.DestinationGuard.capture(target, destination) if reserved else None
    settings = Settings(scope, quality, keep_video, destination)
    job = jobs.Job.create(job_id, settings, planned_items(entries, scope == "playlist"),
                          enqueued_base=str(target) if reserved else None)
    return job, deps, log, guard


def create_destination(target, scope, info, address, *, queue=False):
    target.mkdir(parents=True, exist_ok=True)
    if scope == "single":
        return target
    generated = add_playlist_directory(target, info.get("title"), address.playlist)
    if queue:
        jobs.safe_destination(generated)
    return jobs.local_directory(generated)


async def retry(job_id, paths, prefs, *, emit: Emit, decide: Decide | None = None, backend_ready=False):
    """Resume an existing plan using only the saved item IDs and destination."""
    store = jobs.JobStore(paths.state)
    job_id = store.select(None).id if job_id is None else job_id
    with store.claim(job_id):
        job = store.load(job_id)
        if job.enqueued_base is not None:
            job = store.load(job_id, admit=True)
        log = Log(store.logs / f"{job.id}.log")
        destination = Path(job.destination)
        jobs.inspect_destination(job)
        destination.parent.mkdir(parents=True, exist_ok=True)
        jobs.local_directory(destination)
        guard = jobs.DestinationGuard.capture(Path(job.enqueued_base), destination) if job.enqueued_base else None
        try:
            return await execute(job, store, paths, prefs, emit, decide, log,
                                 guard=guard, backend_ready=backend_ready)
        except DecisionDismissed:
            raise AttemptDismissed(planned=True) from None


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


def video_destination(job, item, source, pending, store):
    if "video" not in pending:
        return None
    target = pending["video"] or Path(job.destination) / f"{item.stem}{jobs.video_suffix(source)}"
    if "video" not in item.outputs and target.name != item.video_target:
        item.video_target = target.name
        try:
            store.save(job)
        except OSError as exc:
            raise YdlError(f"Could not save the selected video target {target}: {exc}") from exc
    if os.path.lexists(target):
        raise jobs.collision(target)
    return target


async def staged_outputs(job, pending, source, deps, staging, target, log):
    outputs = {}
    if "mp3" in pending:
        mp3 = staging / "verified.mp3"
        await media.convert(deps.ffmpeg, source, mp3, job.quality, log)
        await media.verify_output(deps.ffprobe, mp3, "mp3", log)
        outputs["mp3"] = mp3, pending["mp3"]
    if target is not None:
        suffix = jobs.video_suffix(source)
        video = source
        if target.suffix != suffix:
            video = staging / f"remuxed{target.suffix}"
            await media.remux(deps.ffmpeg, source, video, log)
        await media.verify_output(deps.ffprobe, video, "video", log)
        outputs["video"] = video, target
    return outputs


async def process_item(job, item, pending, store, deps, emit, log, guard):
    if guard:
        guard.check()
    staging = jobs.staging_path(job, item)
    source = await staged_source(job, item, deps, emit, store, staging, log)
    emit(item_event(job, item, "converting"))
    target = video_destination(job, item, source, pending, store)
    outputs = await staged_outputs(job, pending, source, deps, staging, target, log)
    await jobs.publish(store, job, item, outputs, guard)
    if guard:
        guard.check()
    try:
        jobs.remove_staging(staging)
    except OSError as exc:
        log.write(f"Could not remove staging files after publication: {exc}")


async def check_items(job):
    checked = []
    for item in job.items:
        try:
            if not item.video_id:
                raise YdlError("The playlist entry has no usable video ID.")
            checked.append((item, await jobs.pending_outputs(job, item), None))
        except (OSError, YdlError) as exc:
            checked.append((item, {}, exc))
    return checked


async def execute(job, store, paths, prefs, emit, decide, log, deps=None, *,
                  guard=None, backend_ready=False):
    emit(Event("job", job.id))
    emit(Event("checking", "Checking output files"))
    checked = await check_items(job)
    if deps is None and any(pending for _, pending, _ in checked):
        deps = await prepare(paths, prefs, decide, emit, log, backend_ready=backend_ready)
    title = Path(job.destination).name if job.scope == "playlist" else job.items[0].title
    emit(Event("settings", settings=job.settings, title=title, total=len(job.items)))
    return await execute_items(job, store, checked, deps, emit, log, guard)


async def execute_items(job, store, checked, deps, emit, log, guard):
    diagnostics = []
    conflicts = []
    changed = ""
    saved = 0
    for item, pending, error in checked:
        try:
            if error:
                raise error
            if pending:
                await process_item(job, item, pending, store, deps, emit, log, guard)
            saved += 1
            emit(item_event(job, item, "saved"))
        except (OSError, TimeoutError, YdlError) as exc:
            detail = str(exc) or "Media verification timed out."
            message = f"{item.title}: {detail}"
            log.write(message)
            diagnostics.append(message)
            emit(item_event(job, item, "failed", message=detail))
            if isinstance(exc, jobs.OutputConflict):
                conflicts.append(detail)
            if isinstance(exc, jobs.DestinationChanged):
                changed = detail
                break
    if guard and not changed:
        try:
            guard.check()
        except jobs.DestinationChanged as exc:
            changed = str(exc)
            diagnostics.append(changed)
            log.write(changed)
    job.complete = not diagnostics
    store.save(job)
    attention = "\n".join([*conflicts, changed] if changed else conflicts)
    return Result(Path(job.destination), saved, len(diagnostics), tuple(diagnostics),
                  job_id=job.id, log=log.path, attention=attention, destination_changed=bool(changed),
                  output_conflict=bool(conflicts))
