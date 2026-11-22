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
from .terminal import clean, clip, display_path, metrics

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


def row_metadata(entry, view, plan):
    observed = view.inspection
    job = observed.job if observed else None
    if job:
        title = Path(job.destination).name if job.scope == "playlist" else job.items[0].title
        kind = f"Playlist ({len(job.items)})" if job.scope == "playlist" else "Video"
        return title, kind, display_path(observed.output) if observed.output else "—"
    if plan and plan.settings:
        settings = plan.settings
        kind = f"Playlist ({plan.total})" if settings.scope == "playlist" else "Video"
        return plan.title, kind, display_path(settings.destination) if settings.scope == "playlist" else "—"
    address = media.parse_url(entry.url)
    if entry.scope == "playlist" or not address.video:
        return f"Playlist {address.playlist}", "Playlist", "—"
    if entry.scope == "single" or not address.playlist:
        return f"Video {address.video}", "Video", "—"
    return f"Video {address.video} + playlist {address.playlist}", "Video + playlist", "—"


def event_label(event):
    return STAGES.get(event.stage, event.stage.capitalize()) if event else "Checking"


def transfer_text(event, phase):
    if event is None:
        return Text(phase or "No active transfer", style="#a6badc")
    title = clean(event.title or event.message or "—")
    position = f" · Playlist {event.position}/{event.total}" if event.position is not None else ""
    bar = Text(f"{event_label(event)} · {title}{position}\nTrack progress ")
    if event.percent is None:
        bar.append("◌  Working…", "#73c7ff")
    else:
        full = min(18, int(event.percent * 18 / 100))
        bar.append("━" * full, "#49e18b")
        bar.append("━" * (18 - full), "#294561")
        bar.append(metrics(event))
    if event.percent is None:
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
        event.stop()


class Dashboard(Screen):
    def __init__(self):
        super().__init__()
        self.rendered = ()
        self.compact_layout = None
        self.table_columns = ()
        self.position = 0

    def compose(self):
        try:
            ver = f"  v{version('ydl')}"
        except PackageNotFoundError:
            ver = ""
        with Horizontal(id="header"):
            yield Static(f"▶  ydl{ver}", id="brand", markup=False)
            yield Static("Backend · Check pending", id="backend-status", markup=False)
            yield button("Retry backend", "retry-backend")
        with VerticalScroll(id="workspace"):
            with Vertical(id="form", classes="panel"):
                with Horizontal(classes="form-line url-line"):
                    yield TextArea(self.app.prefill or "", id="draft", compact=True,
                                   placeholder="Paste YouTube URLs · one per line", soft_wrap=False)
                    yield button("＋ Add to Queue", "add-urls", "primary")
                with Horizontal(classes="form-line"):
                    yield label("Scope", "field-label")
                    yield select(SCOPE_CHOICES, "auto", "scope")
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
                yield JobTable(id="queue", cursor_type="row", show_row_labels=False)
            with Vertical(id="transfer-panel", classes="panel"):
                yield Static("Active transfer", classes="section-title", markup=False)
                yield Static("No active transfer", id="transfer")
        with Vertical(id="action-area"):
            with Horizontal(id="controls"):
                yield button("Start queue", "primary", "primary")
                yield button("Retry failed", "retry-failed")
                yield button("Clear completed", "clear-completed")
                yield button("Settings", "settings")
                yield button("Recent jobs", "recent")
                yield button("Help", "help")
                yield button("Quit", "quit")
            with Horizontal(id="status-line"):
                yield Static("", id="notice", markup=False)
                yield button("Recover queue", "recover", "warning")

    def on_mount(self):
        self.app.call_after_refresh(self.app.startup, self)

    def on_resize(self):
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
        self.query_one("#backend-status", Static).update(format_backend(session.backend.status))
        notice = session.index.error or session.backend.config_error or session.index.notice or session.notice
        self.query_one("#notice", Static).update(clean(notice or session.backend.status.detail or
                                                       "Tab navigate · Enter select · Ctrl+C stop · Ctrl+Q quit"))
        counts = Counter(session.index.view(row.id).status for row in session.index.rows)
        event = session.events.get(session.active_id)
        active = f" · Active {event.position}/{event.total}" if event and event.position else ""
        if counts["Active"] and not active:
            active = " · 1 Active"
        interrupted = f" · {counts['Interrupted']} Interrupted" if counts["Interrupted"] else ""
        self.query_one("#queue-summary", Static).update(f"Queue · {len(session.index.rows)} URLs{active} · "
                                                        f"{counts['Pending']} Pending{interrupted} · "
                                                        f"{counts['Complete']} Complete · {counts['Failed']} Failed")
        self.query_one("#transfer", Static).update(transfer_text(event, "Preparing backend…" if session.busy else ""))

    def columns(self, width):
        if width >= 100:
            return (("#", "position", 3), ("Title", "title", max(17, width - 83)),
                    ("Type", "type", 16), ("Status", "status", 16), ("Track progress", "progress", 14),
                    ("Output", "output", 15), ("⋮", "actions", 2))
        return (("#", "position", 3), ("Title", "title", max(9, width - 32)),
                ("Status", "status", 14), ("⋮", "actions", 2))

    def row_cells(self, session, row, position, columns):
        view = session.index.view(row.id)
        event = session.events.get(row.id) if session.active_id == row.id else None
        status = event_label(event) if view.status == "Active" else view.status
        title, kind, output = row_metadata(row, view, session.plans.get(row.id))
        progress = f"{event.percent:.0f}%" if event and event.percent is not None else "◌" if event else "—"
        values = dict(position=str(position), title=title, type=kind, status=status, output=output,
                      progress="100%" if view.status == "Complete" else progress, actions="⋮")
        return tuple(clip(clean(values[key]), size) for _, key, size in columns)

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
            table.add_row(*(self.cell(key, value) for (_, key, _), value in zip(columns, values)), key=row_id)
        self.position = next((i for i, (row_id, _) in enumerate(cells) if row_id == selected), 0)
        if cells:
            table.move_cursor(row=self.position, scroll=False)
            table.scroll_to(y=offset, animate=False)

    def update_cells(self, table, row_id, columns, values):
        for (_, key, _), value in zip(columns, values):
            table.update_cell(row_id, key, self.cell(key, value))

    @staticmethod
    def cell(key, value):
        style = STATUS_COLORS.get(value, "") if key == "status" else ""
        if key == "actions":
            style = "bold #86cbff"
        return Text(value, style=style, no_wrap=True)
