"""Queue row actions, saved job browsing and read-only job details."""

import asyncio
from pathlib import Path

from rich.table import Table
from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, DataTable, Static, TabbedContent, TabPane

from .terminal import clean, clean_lines, clip, display_path, format_eta, format_speed
from .tui_dialogs import DialogTabs, Panel
from .tui_widgets import JobTable, button, event_label, job_title, progress_bar, row_metadata

DASH = "—"
ROW_ACTIONS = (("details", "Details"), ("retry-selected", "Retry selected entry"),
               ("up", "Move up"), ("down", "Move down"), ("remove", "Remove selected"),
               ("recheck", "Recheck"))


class RowMenu(Panel):
    heading = "Queue row actions"
    initial_focus = "#details"
    BINDINGS = [Binding("up", "previous", show=False), Binding("down", "next", show=False)]

    def __init__(self, row_id):
        super().__init__()
        self.row_id = row_id

    def body(self):
        for key, title in ROW_ACTIONS:
            yield button(title, key)

    def sync(self):
        super().sync()
        for key, _ in ROW_ACTIONS:
            self.query_one(f"#{key}", Button).disabled = not self.app.can_row(key, self.row_id)

    def action_previous(self):
        self.focus_previous()

    def action_next(self):
        self.focus_next()

    @on(Button.Pressed)
    def row_action(self, event):
        action = event.button.id
        if action in dict(ROW_ACTIONS) and self.app.can_row(action, self.row_id):
            self.dismiss(action)


class RecentJobs(Panel):
    heading = "Recent jobs"
    initial_focus = "#recent-table"

    def __init__(self):
        super().__init__()
        self.ids = []
        self.selected = None
        self.is_loading = True

    def body(self):
        table = JobTable(id="recent-table", cursor_type="row", show_row_labels=False)
        table.add_column("Title / Job ID", width=31, key="title")
        table.add_column("Status", width=16, key="status")
        yield table
        yield Static("Reading saved jobs…", id="recent-note", markup=False)

    def buttons(self):
        yield button("Details", "recent-details")
        yield button("Retry selected job", "recent-retry", "primary")
        yield from super().buttons()

    def on_mount(self):
        super().on_mount()
        self.app.own(self.load_jobs())

    async def load_jobs(self):
        session = self.app.session
        try:
            self.ids = await asyncio.to_thread(session.store.recent_ids)
        except OSError as exc:
            if self.is_mounted:
                self.query_one("#recent-note", Static).update(clean(f"Cannot list {session.store.directory}: {exc}"))
            return
        if not self.is_mounted or session.closing:
            return
        self.is_loading = False
        table = self.query_one(JobTable)
        self.selected = self.ids[0] if self.ids else None
        refresh = {}
        for row_id in self.ids:
            table.add_row(row_id, "Verifying", key=row_id)
            if session.index.row(row_id) is not None or session.external_retry_id == row_id:
                continue
            # A verification can outlive the panel that requested it.
            if row_id in session.external and session.external[row_id] is None:
                continue
            refresh[row_id] = session.external.get(row_id)
            session.external[row_id] = None
        self.sync()
        await asyncio.gather(*(self.verify(session, row_id, previous) for row_id, previous in refresh.items()))

    async def verify(self, session, row_id, previous):
        result = await session.inspect(row_id)
        if result != previous:
            session.external_results.pop(row_id, None)
        session.external[row_id] = result
        session.changed()

    def row_info(self, row_id):
        session = self.app.session
        entry = session.index.row(row_id)
        status = job_status(session, row_id)
        if entry:
            view = session.index.view(row_id)
            title, _, _ = row_metadata(entry, view, session.plans.get(row_id))
            return title, status, view.diagnostic
        report = session.external.get(row_id)
        job = report.job if report else None
        title = job_title(job) if job else row_id
        return title, status, report.diagnostic if report else ""

    def sync(self):
        super().sync()
        table = self.query_one(JobTable)
        for row_id in self.ids:
            title, status, _ = self.row_info(row_id)
            table.update_cell(row_id, "title", Text(clip(clean(title), 31)))
            table.update_cell(row_id, "status", Text(status))
        report = self.row_info(self.selected)[2] if self.selected else ""
        fallback = "Reading saved jobs…" if self.is_loading else "No saved jobs" if not self.ids else ""
        self.query_one("#recent-note", Static).update(clean(report or fallback))
        self.query_one("#recent-details", Button).disabled = not self.selected
        self.query_one("#recent-retry", Button).disabled = not can_retry(self.app.session, self.selected)

    @on(DataTable.RowHighlighted)
    def highlight(self, event):
        self.selected = event.row_key.value
        self.sync()

    @on(DataTable.RowSelected)
    def row_entered(self, event):
        self.app.details(event.row_key.value, recent=True)

    @on(JobTable.Open)
    def row_clicked(self, event):
        if not event.menu:
            self.app.details(event.row_id, recent=True)

    @on(Button.Pressed, "#recent-details")
    def details(self):
        if self.selected:
            self.app.details(self.selected, recent=True)

    @on(Button.Pressed, "#recent-retry")
    def retry(self):
        retry_job(self.app.session, self.selected)


def can_retry(session, row_id):
    if not row_id:
        return False
    if session.index.row(row_id) is not None:
        return session.media_allowed and session.selected_retry(row_id)
    observed = session.external.get(row_id)
    return bool(session.external_allowed and observed and observed.state == "unfinished")


def retry_job(session, row_id):
    if can_retry(session, row_id):
        session.arm("selected" if session.index.row(row_id) is not None else "external", row_id)


def job_status(session, row_id):
    if session.active_id == row_id:
        return "Active"
    if session.index.row(row_id) is not None:
        return session.index.view(row_id).status
    observed = session.external.get(row_id)
    if not observed:
        return "Verifying"
    return {"complete": "Complete", "unfinished": "Incomplete"}.get(observed.state, "Needs attention")


def tail_log(path):
    limit = 64 * 1024
    with path.open("rb") as stream:
        length = stream.seek(0, 2)
        stream.seek(max(0, length - limit))
        raw = stream.read(limit)
    lines = raw.decode(errors="replace").splitlines()
    omitted = length > limit or len(lines) > 200
    return ("Older text omitted\n" if omitted else "") + clean_lines("\n".join(lines[-200:]))


def active_progress(event):
    stage = event_label(event)
    if not event:
        return [("Status", stage)]
    if event.position is not None:
        stage += f" ({event.position}/{event.total})"
    progress = Text("◌  Working…", style="#73c7ff")
    if event.percent is not None:
        progress = progress_bar(event.percent, 18)
        progress.append(f" {event.percent:.0f}%")
    return [("Status", stage), ("Track", event.title or DASH), ("Track progress", progress),
            ("Speed", format_speed(event.speed) if event.speed is not None else DASH),
            ("ETA", format_eta(event.eta) if event.eta is not None else DASH)]


class Details(Panel):
    heading = "Job Details"
    initial_focus = "#--content-tab-progress-pane"

    def __init__(self, row_id, *, recent=False):
        super().__init__()
        self.row_id, self.recent = row_id, recent
        self.log_loaded = False
        self.log_reading = False
        self.has_log = False

    def body(self):
        with DialogTabs(id="details-tabs"):
            with TabPane("Progress", id="progress-pane"):
                yield Static("", id="details-progress")
            with TabPane("Log", id="log-pane"):
                yield Static("", id="log-path", markup=False)
                with Horizontal(classes="form-line"):
                    yield button("Refresh", "log-refresh")
                    yield button("Copy log path", "copy-log")
                yield Static("", id="log-text", markup=False)
            with TabPane("Info", id="info-pane"):
                yield Static("", id="details-info", markup=False)

    def buttons(self):
        yield button("Retry selected entry", "details-retry", "primary")
        yield from super().buttons()

    def sync(self):
        super().sync()
        session, row_id = self.app.session, self.row_id
        entry = session.index.row(row_id)
        if entry:
            view = session.index.view(row_id)
            inspected, result = view.inspection, view.result
        else:
            view = None
            inspected = session.external.get(row_id)
            result = session.external_results.get(row_id) if inspected else None
        job = inspected.job if inspected else None
        plan = session.plans.get(row_id)
        settings = job.settings if job else plan.settings if plan else None
        title = row_metadata(entry, view, plan)[0] if entry else job_title(job) if job else row_id
        self.query_one(".dialog-title", Static).update(f"Job Details — {clean(title)}")
        status = job_status(session, row_id)
        diagnostic = self.diagnostic(entry, view, inspected, result)
        retry = self.query_one("#details-retry", Button)
        retry.label = "Retry selected entry" if entry else "Retry selected job"
        retry.disabled = not can_retry(session, row_id)
        progress = self.progress_fields(status, session.events.get(row_id), result, inspected, settings, diagnostic)
        self.query_one("#details-progress", Static).update(progress)
        self.query_one("#details-info", Static).update(self.info_text(entry, inspected, job, plan, settings, diagnostic))
        self.query_one("#log-path", Static).update(clean(self.log_path))
        self.query_one("#copy-log", Button).disabled = not self.has_log
        self.query_one("#log-refresh", Button).disabled = self.log_reading

    @staticmethod
    def diagnostic(entry, view, inspected, result):
        if view and view.diagnostic:
            return view.diagnostic
        if inspected and inspected.diagnostic:
            return inspected.diagnostic
        if entry and entry.diagnostic:
            return entry.diagnostic
        return "\n".join(result.diagnostics) if result else ""

    @staticmethod
    def progress_fields(status, event, result, inspected, settings, diagnostic):
        fields = [("Status", status)]
        if status == "Active":
            fields = active_progress(event)
        else:
            if inspected and inspected.job:
                fields.extend((("Verified", f"{inspected.verified}/{len(inspected.job.items)}"),
                               ("Track progress", "100%" if status == "Complete" else DASH)))
            if result:
                fields.extend((("Saved", str(result.saved)), ("Failed", str(result.failed))))
        output = ((inspected.output if inspected else None) or (result.destination if result else None)
                  or (settings.destination if settings else None))
        if output:
            fields.append(("Output", display_path(output)))
        if settings:
            quality = f"VBR {settings.quality}" if len(settings.quality) == 1 else settings.quality
            fields.extend((("MP3 quality", quality), ("Keep video", "Yes" if settings.keep_video else "No")))
        if diagnostic and status not in ("Active", "Verifying", "Complete"):
            fields.append(("Diagnostic", Text(clean_lines(diagnostic), style="#ffba68")))
        grid = Table.grid(padding=(0, 1), pad_edge=False)
        grid.add_column(width=14, style="#c6ddff")
        grid.add_column()
        for name, value in fields:
            grid.add_row(name, value if isinstance(value, Text) else Text(clean(value)))
        return grid

    def info_text(self, entry, inspected, job, plan, settings, diagnostic):
        values = dict.fromkeys(("Reserved queue ID", "Core job ID", "Canonical URL", "Requested scope", "Type",
                                "Resolved scope", "MP3 quality", "Keep video", "Enqueued base", "Resolved output"))
        values["Last diagnostic"] = diagnostic
        if entry:
            _, kind, _ = row_metadata(entry, self.app.session.index.view(entry.id), plan)
            values.update({"Reserved queue ID": entry.id, "Canonical URL": entry.url,
                           "Requested scope": entry.scope, "Enqueued base": entry.output, "Type": kind})
        options = settings or entry
        if options:
            values.update({"MP3 quality": options.quality, "Keep video": "Yes" if options.keep_video else "No"})
        if settings:
            output = (inspected.output if inspected else None) or settings.destination
            values.update({"Core job ID": self.row_id, "Resolved scope": settings.scope,
                           "Resolved output": display_path(output)})
        if job and not entry:
            kind = f"Playlist ({len(job.items)})" if job.scope == "playlist" else "Video"
            values.update({"Type": kind, "Enqueued base": job.enqueued_base})
        return "\n".join(f"{name}  {clean_lines(value or DASH)}" for name, value in values.items())

    @property
    def log_path(self):
        return self.app.session.store.logs / f"{self.row_id}.log"

    @on(TabbedContent.TabActivated)
    def tab(self, event):
        if event.pane.id == "log-pane" and not self.log_loaded:
            self.refresh_log()

    @on(Button.Pressed, "#log-refresh")
    def refresh_log(self):
        if not self.log_reading:
            self.log_reading = True
            self.app.own(self.load_log())

    async def load_log(self):
        try:
            text = await asyncio.to_thread(tail_log, self.log_path)
            self.has_log = True
        except FileNotFoundError:
            text = "No log yet"
            self.has_log = False
        except OSError as exc:
            text = clean(f"Cannot read {self.log_path}: {exc}. Check its permissions and Refresh.")
            self.has_log = False
        self.log_loaded = True
        self.log_reading = False
        if self.is_mounted:
            self.query_one("#log-text", Static).update(text)
            self.sync()

    @on(Button.Pressed, "#copy-log")
    def copy_log(self):
        if self.has_log:
            self.app.copy_to_clipboard(str(self.log_path))

    @on(Button.Pressed, "#details-retry")
    def retry(self):
        retry_job(self.app.session, self.row_id)
