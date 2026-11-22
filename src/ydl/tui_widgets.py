"""Terminal widgets and presentation of queue rows and transfer events."""

from collections import Counter
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from rich.text import Text
from textual import events, on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Select, Static, TextArea

from . import media
from .terminal import clean, clean_lines, clip, metrics

SCOPE_CHOICES = [("Auto", "auto"), ("Single video", "single"), ("Entire playlist", "playlist")]
STAGES = {"checking": "Checking", "updating": "Updating", "metadata": "Fetching metadata",
          "downloading": "Downloading", "converting": "Converting", "saved": "Saved", "failed": "Failed"}
STATUS_COLORS = {"Complete": "#49e18b", "Failed": "#ff7c85", "Needs attention": "#ffba68",
                 "Active": "#71cfff", "Interrupted": "#f2ca76"}


def button(label, key, variant="default"):
    return Button(label, id=key, variant=variant, compact=True)


def quality_options(current):
    values = [str(i) for i in range(10)] + ["192K", "256K", "320K"]
    if current not in values:
        values.append(current)
    return [(f"VBR {value}" if len(value) == 1 else value, value) for value in values]


def select(options, choice, key):
    return Select(options, value=choice, allow_blank=False, id=key, compact=True)


def label(text, width=None):
    return Label(text, markup=False, classes=width)


def format_backend(status):
    version_text = f" (yt-dlp {status.version})" if status.version else ""
    return f"Backend · {status.state}{version_text}"


def move_target(rows, row_id, direction, eligible):
    row = next((i for i, entry in enumerate(rows) if entry.id == row_id and eligible(entry)), None)
    if row is None:
        return None
    stop = len(rows) if direction > 0 else -1
    return next(((row, i) for i in range(row + direction, stop, direction) if eligible(rows[i])), None)


def job_title(job):
    return Path(job.destination).name if job.scope == "playlist" else job.items[0].title


def row_metadata(entry, view, plan):
    observed = view.inspection
    job = observed.job if observed else None
    if job:
        kind = f"Playlist ({len(job.items)})" if job.scope == "playlist" else "Video"
        output = observed.output.name if observed.output else "—"
        if job.scope == "playlist" and observed.output:
            output += "/"
        return job_title(job), kind, output
    if plan and plan.settings:
        settings = plan.settings
        kind = f"Playlist ({plan.total})" if settings.scope == "playlist" else "Video"
        return plan.title, kind, f"{settings.destination.name}/" if settings.scope == "playlist" else "—"
    address = media.parse_url(entry.url)
    if entry.scope == "playlist" or not address.video:
        return f"Playlist {address.playlist}", "Playlist", "—"
    if entry.scope == "single" or not address.playlist:
        return f"Video {address.video}", "Video", "—"
    return f"Video {address.video} + playlist {address.playlist}", "Video + playlist", "—"


def event_label(event):
    return STAGES.get(event.stage, event.stage.capitalize()) if event else "Checking"


def progress_bar(percent, width):
    full = max(0, min(width, int(percent * width / 100)))
    bar = Text("━" * full, style="#49e18b")
    bar.append("─" * (width - full), "#7293b6")
    return bar


def transfer_text(event, phase, bar_width=18):
    if event is None:
        return Text(phase or "No active transfer", style="#a6badc")
    title = clean(event.title or event.message or "—")
    position = f" · Playlist {event.position}/{event.total}" if event.position is not None else ""
    bar = Text(f"{event_label(event)}{position} · {title}\nTrack progress ")
    if event.percent is None:
        bar.append("◌  Working…", "#73c7ff")
    else:
        bar.append(progress_bar(event.percent, bar_width))
    bar.append(metrics(event))
    return bar


class JobTable(DataTable):
    class Open(Message):
        def __init__(self, table, row_id, menu):
            super().__init__()
            self.table, self.row_id, self.menu = table, row_id, menu

        @property
        def control(self):
            return self.table

    async def _on_click(self, event: events.Click):
        # Textual attaches table cell coordinates to the rendered cell's style.
        meta = event.style.meta
        row = meta.get("row", -1)
        if not 0 <= row < self.row_count:
            return
        self.move_cursor(row=row, scroll=False)
        row_id = self.ordered_rows[row].key.value
        column = meta.get("column", -1)
        actions = column >= 0 and self.ordered_columns[column].key.value == "actions"
        if actions or event.button == 3 or event.chain >= 2:
            self.post_message(self.Open(self, row_id, actions or event.button == 3))
        event.prevent_default()
        event.stop()


class Dashboard(Screen):
    VERTICAL_BREAKPOINTS = [(35, "-tall")]

    def __init__(self):
        super().__init__()
        self.rendered = ()
        self.table_columns = ()
        self.position = 0
        self.notice_text = None

    def compose(self):
        brand = Text()
        brand.append(" ▶ ", "bold white on #2159d8")
        brand.append("  ydl", "bold #e4efff")
        try:
            installed = version("ydl")
        except PackageNotFoundError:
            pass
        else:
            brand.append("  ")
            brand.append(f" v{installed} ", "bold #e4efff on #1d55a0")
        with Horizontal(id="header"):
            yield Static(brand, id="brand")
            yield Static("YouTube to MP3", id="tagline", markup=False)
            yield Static("Backend · Check pending", id="backend-status", markup=False)
            yield button("Retry backend", "retry-backend")
            yield button("⚙ Settings", "settings")
        with VerticalScroll(id="workspace"):
            with Vertical(id="form", classes="panel"):
                with Horizontal(classes="form-line url-line"):
                    yield TextArea(self.app.prefill or "", id="draft", compact=True,
                                   placeholder="Paste YouTube URLs · one per line", soft_wrap=False, tab_behavior="focus")
                    yield button("＋ Add to Queue", "add-urls", "primary")
                with Vertical(id="form-options"):
                    with Vertical(id="media-options"):
                        with Horizontal(classes="form-line"):
                            yield label("Scope", "field-label")
                            yield select(SCOPE_CHOICES, "auto", "scope")
                        with Horizontal(classes="form-line"):
                            yield label("MP3 quality", "field-label")
                            yield select(quality_options("0"), "0", "quality")
                        yield Checkbox("Keep video", id="retain", compact=True)
                    with Horizontal(classes="form-line"):
                        yield label("Save to", "field-label")
                        yield Input(id="destination", compact=True)
                        yield button("Browse", "browse")
                yield Static("", id="form-error", markup=False)
            with Vertical(id="queue-panel", classes="panel"):
                yield Static("Queue · 0 URLs", id="queue-summary", markup=False)
                yield JobTable(id="queue", cursor_type="row", show_row_labels=False,
                               cursor_foreground_priority="renderable")
            with Vertical(id="transfer-panel", classes="panel"):
                yield Static("Active transfer", id="transfer-title", classes="section-title", markup=False)
                yield Static("No active transfer", id="transfer")
            with Vertical(id="action-area"):
                with Horizontal(id="controls"):
                    yield button("Start queue", "primary", "primary")
                    yield button("Retry failed", "retry-failed")
                    yield button("Clear completed", "clear-completed")
                    yield button("Recent jobs", "recent")
                    yield button("Help", "help")
                    yield button("Quit", "quit")
                with Horizontal(id="status-line"):
                    with VerticalScroll(id="notice-scroll"):
                        yield Static("", id="notice", markup=False)
                    yield button("Recover queue", "recover", "warning")

    def on_mount(self):
        self.app.call_after_refresh(self.app.startup, self)

    def on_resize(self, event):
        self.set_class(event.size.width >= 120, "-inline-destination")
        self.call_after_refresh(self.app.refresh_views)

    @on(Button.Pressed)
    def button_pressed(self, event):
        self.app.dashboard_button(event.button.id)

    def show_defaults(self, prefs):
        self.query_one("#destination", Input).value = prefs.output
        quality = self.query_one("#quality", Select)
        quality.set_options(quality_options(prefs.quality))
        quality.value = prefs.quality
        self.query_one("#retain", Checkbox).value = prefs.keep_video

    def update_controls(self, session):
        index = session.index
        primary = self.query_one("#primary", Button)
        resumable = any(index.view(row.id).status == "Interrupted" for row in index.rows)
        idle_label = "Resume queue" if resumable else "Start queue"
        primary.label = "Stopping…" if session.stopping else "Stop queue" if session.busy else idle_label
        primary.variant = "error" if session.busy else "primary"
        primary.disabled = session.stopping or (not session.busy and
                                                not (session.media_allowed and session.candidates("start")))
        self.query_one("#retry-failed", Button).disabled = not (session.media_allowed and session.candidates("failed"))
        self.query_one("#clear-completed", Button).disabled = not (index.editable and not session.busy and
                                                                  any(index.view(row.id).status == "Complete" for row in index.rows))
        self.query_one("#add-urls", Button).disabled = not index.editable or bool(session.backend.config_error)
        retry = self.query_one("#retry-backend", Button)
        retry.display = bool(session.backend_retry_visible)
        retry.disabled = not session.can_retry_backend
        recover = self.query_one("#recover", Button)
        recover.display = bool(index.error)
        recover.disabled = not session.can_recover

    def update_status(self, session):
        backend = session.backend.status
        color = "#ff7c85" if backend.state == "Unavailable" else "#a6daae" if backend.version else "#c6ddff"
        self.query_one("#backend-status", Static).update(Text(format_backend(backend), style=color))
        self.update_notice(session)
        counts = Counter(session.index.view(row.id).status for row in session.index.rows)
        event = session.events.get(session.active_id)
        active = f" · Active {event.position}/{event.total}" if event and event.position else ""
        if counts["Active"] and not active:
            active = " · 1 Active"
        interrupted = f" · {counts['Interrupted']} Interrupted" if counts["Interrupted"] else ""
        self.query_one("#queue-summary", Static).update(f"Queue · {len(session.index.rows)} URLs{active} · "
                                                        f"{counts['Pending']} Pending{interrupted} · "
                                                        f"{counts['Complete']} Complete · {counts['Failed']} Failed")
        plan = session.plans.get(session.active_id)
        title = f"Active transfer · {clean(plan.title)}" if plan and plan.title else "Active transfer"
        self.query_one("#transfer-title", Static).update(title)
        bar_width = min(48, max(18, self.size.width // 4))
        self.query_one("#transfer", Static).update(
            transfer_text(event, "Preparing backend…" if session.busy else "", bar_width))

    def update_notice(self, session):
        index, backend = session.index, session.backend
        error = index.error or backend.config_error
        action = index.notice or session.notice
        detail = backend.status.detail
        notice = error or "\n".join(filter(None, (detail, action))) or index.restore_notice
        text = clean_lines(notice or "Tab navigate · Enter select · Ctrl+C stop · Ctrl+Q quit")
        scroll = self.query_one("#notice-scroll", VerticalScroll)
        scroll.set_class(bool(error or detail), "-diagnostic")
        scroll.can_focus = bool(notice)
        if text != self.notice_text:
            self.query_one("#notice", Static).update(text)
            scroll.scroll_home(animate=False, immediate=True)
            self.notice_text = text

    def columns(self, width):
        if width >= 100:
            extra = width - 100
            progress = extra // 5
            output = extra * 9 // 20
            return (("#", "position", 3), ("Title", "title", 14 + extra - progress - output),
                    ("Type", "type", 16), ("Status", "status", 20), ("Track progress", "progress", 14 + progress),
                    ("Output", "output", 14 + output), ("⋮", "actions", 2))
        return (("#", "position", 3), ("Title", "title", max(9, width - 32)),
                ("Status", "status", 14), ("⋮", "actions", 2))

    def row_cells(self, session, row, position, columns):
        view = session.index.view(row.id)
        event = session.events.get(row.id) if view.status == "Active" and session.active_id == row.id else None
        status = event_label(event) if view.status == "Active" else view.status
        if event and event.position is not None and self.size.width >= 100:
            position_text = f" {event.position}/{event.total}"
            status_width = next(size for _, key, size in columns if key == "status")
            status = clip(status, status_width - len(position_text)) + position_text
        title, kind, output = row_metadata(row, view, session.plans.get(row.id))
        percent = 100 if view.status == "Complete" else event.percent if event else None
        values = dict(position=str(position), title=title, type=kind, status=status, output=output,
                      progress="◌" if view.status == "Active" else "—", actions="⋮")
        return tuple(self.cell(key, values[key], size, percent) for _, key, size in columns)

    def draw_table(self, session, selected):
        table = self.query_one("#queue", JobTable)
        rows = session.index.rows
        columns = self.columns(self.size.width or 80)
        cells = tuple((row.id, self.row_cells(session, row, i, columns)) for i, row in enumerate(rows, 1))
        previous = tuple(row_id for row_id, _ in self.rendered)
        current = tuple(row_id for row_id, _ in cells)
        if self.table_columns != columns or previous != current:
            self.rebuild_table(table, columns, cells, selected)
        elif self.rendered != cells:
            for (row_id, before), (_, after) in zip(self.rendered, cells):
                if before != after:
                    self.update_cells(table, row_id, columns, after)
        self.table_columns, self.rendered = columns, cells

    def rebuild_table(self, table, columns, cells, selected):
        offset = table.scroll_y
        table.clear(columns=True)
        for title, key, size in columns:
            table.add_column(title, width=size, key=key)
        for row_id, values in cells:
            table.add_row(*values, key=row_id)
        self.position = next((i for i, (row_id, _) in enumerate(cells) if row_id == selected), 0)
        if cells:
            table.move_cursor(row=self.position, scroll=False)
            table.scroll_to(y=offset, animate=False)

    def update_cells(self, table, row_id, columns, values):
        for (_, key, _), value in zip(columns, values):
            table.update_cell(row_id, key, value)

    @staticmethod
    def cell(key, value, size, percent):
        if key == "progress" and percent is not None:
            bar = progress_bar(percent, size - 5)
            bar.append(f" {percent:>3.0f}%")
            return bar
        style = STATUS_COLORS.get(value, "") if key == "status" else ""
        if key == "actions":
            style = "bold #86cbff"
        return Text(clip(clean(value), size), style=style, no_wrap=True)
