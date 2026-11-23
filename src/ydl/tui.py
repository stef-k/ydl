"""Optional Textual launcher and application-owned queue and dialog actions."""

import asyncio

from textual import on
from textual.app import App
from textual.binding import Binding
from textual.widgets import DataTable, Input, Select, TextArea

from . import config
from .models import YdlError
from .runner import DecisionDismissed
from .terminal import clean, clean_lines
from .tui_backend import output_directory
from .tui_dialogs import AddURLs, Browse, Confirm, HelpPanel, RunnerPrompt, SettingsPanel
from .tui_jobs import Details, RecentJobs, RowMenu
from .tui_queue import QueueFile
from .tui_session import QueueSession
from .tui_widgets import Dashboard, JobTable, move_target

MAIN_SHORTCUTS = {"add", "retry_selected", "retry_failed", "details", "settings",
                  "row_menu", "remove", "clear_complete", "help"}


class YdlApp(App[int]):
    CSS_PATH = "tui.tcss"
    TITLE = "ydl"
    ENABLE_COMMAND_PALETTE = False
    HORIZONTAL_BREAKPOINTS = [(75, "-inline-options"), (100, "-wide")]
    BINDINGS = [
        Binding("a", "add", show=False), Binding("r", "retry_selected", show=False),
        Binding("R", "retry_failed", show=False), Binding("d", "details", show=False),
        Binding("s", "settings", show=False), Binding("f2", "row_menu", show=False),
        Binding("delete", "remove", show=False), Binding("c", "clear_complete", show=False),
        Binding("question_mark", "help", show=False),
        Binding("ctrl+c", "stop", show=False, priority=True),
        Binding("ctrl+q", "quit", show=False, priority=True),
    ]

    def __init__(self, paths, prefill=None, disk=None):
        super().__init__()
        self.prefill = prefill
        self.dashboard = None
        self.selected = None
        self.tasks = set()
        self.waits = set()
        self.owned_prompt = None
        self.quit_dialog = None
        self.quit_answered = asyncio.Event()
        self.recovery_draft = None
        self.deferred_close = {}
        self.last_stage = None
        self.progress_timer = None
        self.session = QueueSession(paths, disk or QueueFile(paths.state), self.changed, self.decide)

    def get_default_screen(self):
        return Dashboard()

    def startup(self, dashboard):
        self.dashboard = dashboard
        self.theme = "textual-dark"
        self.refresh_views()
        dashboard.query_one("#draft", TextArea).focus()
        self.own(self.session.open())

    def own(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.task_done)
        return task

    def task_done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            self.panic(exc)

    def changed(self, *, defaults=False, progress=False):
        if self.dashboard is None:
            return
        if defaults:
            self.dashboard.show_defaults(self.session.backend.prefs)
        event = self.session.events.get(self.session.active_id)
        stage = (self.session.active_id, event.stage, event.position) if event else None
        if progress and stage == self.last_stage:
            if self.progress_timer is None:
                self.progress_timer = self.set_timer(0.2, self.refresh_views)
            return
        self.last_stage = stage
        self.refresh_views()

    def refresh_views(self):
        if self.dashboard is None:
            return
        if self.progress_timer is not None:
            self.progress_timer.stop()
            self.progress_timer = None
        self.refresh_dashboard()
        for screen in self.screen_stack[1:]:
            if getattr(screen, "ready", False):
                screen.sync()
        if self.screen in self.deferred_close:
            self.screen.dismiss(self.deferred_close.pop(self.screen))

    def refresh_dashboard(self):
        rows = self.session.index.rows
        if self.session.index.row(self.selected) is None:
            pos = min(self.dashboard.position, len(rows) - 1)
            self.selected = rows[pos].id if rows else None
        self.dashboard.update_controls(self.session)
        self.dashboard.update_status(self.session)
        self.dashboard.draw_table(self.session, self.selected)
        self.dashboard.query_one("#draft", TextArea).read_only = self.recovery_draft is not None

    def show(self, screen, callback=None, *, opener=None):
        origin = self.screen
        opener = opener or origin.focused

        def restore(value):
            self.deferred_close.pop(screen, None)
            if self.screen is origin and opener is not None and opener.is_mounted and not opener.disabled:
                origin.set_focus(opener)
            elif self.screen is self.dashboard and not self.session.closing:
                self.dashboard.query_one("#queue").focus()
            if callback:
                callback(value)
            self.refresh_views()

        self.push_screen(screen, restore)

    async def ask(self, screen):
        future = asyncio.get_running_loop().create_future()
        self.waits.add(future)

        def reply(value):
            if not future.done():
                future.set_result(value)

        self.show(screen, reply)
        try:
            return await future
        finally:
            self.waits.discard(future)

    def close_when_current(self, screen, result=None):
        stack = self.screen_stack
        if stack and stack[-1] is screen:
            screen.dismiss(self.deferred_close.pop(screen, result))
        elif screen in stack:
            self.deferred_close[screen] = result

    async def decide(self, decision):
        if self.quit_dialog is not None:
            await self.quit_answered.wait()
        dialog = self.owned_prompt = RunnerPrompt(decision)
        try:
            answer = await self.ask(dialog)
        finally:
            self.owned_prompt = None
            self.close_when_current(dialog)
        if answer is None:
            raise DecisionDismissed()
        return answer

    def browse(self, field):
        def selected(path):
            if path is not None:
                field.value = path
        self.show(Browse(field.value), selected)

    def check_action(self, action, parameters):
        if action not in MAIN_SHORTCUTS:
            return super().check_action(action, parameters)
        if self.dashboard is None or self.screen is not self.dashboard or isinstance(self.focused, (Input, TextArea)):
            return False
        return action != "row_menu" or isinstance(self.focused, JobTable)

    def dashboard_button(self, action):
        dispatch = {"primary": self.primary, "retry-failed": self.action_retry_failed,
                    "clear-completed": self.action_clear_complete, "add-urls": self.action_add,
                    "settings": self.action_settings, "help": self.action_help,
                    "recent": self.show_recent, "quit": self.action_quit,
                    "recover": lambda: self.own(self.recover_queue()),
                    "retry-backend": lambda: self.own(self.session.retry_backend()),
                    "browse": lambda: self.browse(self.dashboard.query_one("#destination", Input))}
        if action in dispatch:
            dispatch[action]()

    @on(DataTable.RowHighlighted, "#queue")
    def highlight(self, event):
        table = event.data_table
        if table.row_count and table.ordered_rows[table.cursor_row].key == event.row_key:
            self.selected = event.row_key.value
            self.dashboard.position = table.cursor_row

    @on(DataTable.RowSelected, "#queue")
    def row_entered(self, event):
        self.details(event.row_key.value)

    @on(JobTable.Open, "#queue")
    def row_clicked(self, event):
        self.selected = event.row_id
        if event.menu:
            self.action_row_menu()
        else:
            self.details(event.row_id)

    def action_add(self):
        if not self.session.index.editable or self.session.backend.config_error:
            return
        draft = self.dashboard.query_one("#draft", TextArea)
        field = self.dashboard.query_one("#destination", Input)
        try:
            output = output_directory(field.value)
        except config.InvalidOutput as exc:
            self.dashboard.set_form_error(clean_lines(exc), field=field)
            return
        quality = self.dashboard.query_one("#quality", Select)
        try:
            selected_quality = config.quality_value(quality.value)
        except YdlError as exc:
            self.dashboard.set_form_error(clean(exc), field=quality)
            return
        self.dashboard.set_form_error("")
        scope = self.dashboard.query_one("#scope", Select).value
        retain = self.dashboard.query_one("#retain").value
        self.show(AddURLs(draft.text, (scope, selected_quality, retain, output)),
                  lambda text: draft.load_text(text) if text is not None else None)

    def primary(self):
        if self.session.busy:
            self.action_stop()
        else:
            self.session.arm("start")

    def action_retry_selected(self):
        self.session.arm("selected", self.selected)

    def action_retry_failed(self):
        self.session.arm("failed")

    def action_settings(self):
        self.own(self.open_settings(self.focused))

    async def open_settings(self, opener):
        if not self.session.backend.config_error:
            await self.session.backend.read()
        if self.screen is self.dashboard and not self.session.closing:
            self.show(SettingsPanel(self.session.backend.prefs), opener=opener)

    def action_help(self):
        self.show(HelpPanel())

    def show_recent(self):
        self.show(RecentJobs())

    def details(self, row_id, *, recent=False):
        if self.session.index.row(row_id) is not None or recent:
            self.show(Details(row_id, recent=recent))

    def action_details(self):
        self.details(self.selected)

    def can_row(self, action, row_id):
        session, index = self.session, self.session.index
        entry = index.row(row_id)
        if entry is None:
            return False
        if action == "details":
            return True
        if action == "retry-selected":
            return session.media_allowed and session.selected_retry(row_id)
        inactive = index.view(row_id).status != "Active"
        if action == "recheck":
            return index.runnable and not session.busy and inactive
        if action == "remove":
            return index.editable and inactive and (not session.busy or self.is_pending(entry))
        direction = -1 if action == "up" else 1
        return index.editable and move_target(index.rows, row_id, direction, self.is_pending) is not None

    def is_pending(self, entry):
        return entry.status == "pending" and self.session.index.view(entry.id).status == "Pending"

    def action_row_menu(self):
        if self.selected is not None:
            row_id = self.selected
            self.show(RowMenu(row_id), lambda action: self.row_action(action, row_id),
                      opener=self.dashboard.query_one("#queue"))

    def row_action(self, action, row_id):
        if action is None or not self.can_row(action, row_id):
            return
        if action == "details":
            self.details(row_id)
        elif action == "retry-selected":
            self.session.arm("selected", row_id)
        elif action == "recheck":
            self.own(self.session.index.recheck(row_id))
        elif action == "remove":
            self.remove(row_id)
        else:
            self.own(self.move(row_id, -1 if action == "up" else 1))

    async def move(self, row_id, direction):
        def swap(rows):
            pair = move_target(rows, row_id, direction, self.is_pending)
            if pair is None:
                return None
            left, right = pair
            updated = rows.copy()
            updated[left], updated[right] = rows[right], rows[left]
            return updated
        await self.session.index.edit(swap)

    def action_remove(self):
        self.remove(self.selected)

    def remove(self, row_id):
        if self.can_row("remove", row_id):
            dialog = Confirm("Remove selected entry", "Remove this queue entry only? Saved jobs, media, logs and partials are kept.", "Remove")
            self.show(dialog, lambda ok: self.own(self.remove_rows(row_id)) if ok else None)

    def action_clear_complete(self):
        if not self.session.index.editable or self.session.busy:
            return
        if any(self.session.index.view(row.id).status == "Complete" for row in self.session.index.rows):
            dialog = Confirm("Clear completed", "Remove completed queue entries only? Saved jobs, media, logs and partials are kept.", "Remove")
            self.show(dialog, lambda ok: self.own(self.remove_rows()) if ok else None)

    async def remove_rows(self, row_id=None):
        def keep(rows):
            if row_id is not None and self.can_row("remove", row_id):
                return [row for row in rows if row.id != row_id]
            if row_id is None and not self.session.busy:
                return [row for row in rows if self.session.index.view(row.id).status != "Complete"]
            return None
        await self.session.index.edit(keep)

    async def recover_queue(self):
        if not await self.session.recover() or self.recovery_draft is None:
            return
        original, accepted = self.recovery_draft
        admitted = {number for number, entry in accepted if self.session.index.row(entry.id) is not None}
        draft = "\n".join(raw for number, raw in enumerate(original.splitlines(), 1) if number not in admitted)
        self.recovery_draft = None
        self.refresh_views()
        for screen in self.screen_stack:
            if isinstance(screen, AddURLs) and screen.recovery_draft is not None:
                screen.recovered(draft)
                return
        editor = self.dashboard.query_one("#draft", TextArea)
        if editor.text == original:
            editor.load_text(draft)

    def action_stop(self):
        if self.session.busy and not self.session.stopping:
            self.session.armed = False
            self.own(self.session.stop())

    def action_quit(self):
        if self.quit_dialog is not None or self.session.closing:
            return
        active = self.session.busy or self.session.backend.check is not None
        draft = self.dashboard.query_one("#draft", TextArea).text
        add_dialog = next((screen for screen in self.screen_stack if isinstance(screen, AddURLs)), None)
        if add_dialog is not None:
            draft = add_dialog.query_one(TextArea).text
        loss = self.session.index.error and (draft.strip() or self.recovery_draft or self.session.index.buffer)
        if active or loss:
            question = "Stop the current operation and quit?" if active else "Quit ydl?"
            if loss:
                question += " Unresolved draft text or buffered outcomes will be discarded."
            self.quit_answered.clear()
            self.quit_dialog = Confirm("Quit ydl", question, "Stop and quit" if active else "Quit", "Stay", quits=True)
            self.show(self.quit_dialog, self.answer_quit)
        else:
            self.answer_quit(True)

    def answer_quit(self, answer):
        self.quit_dialog = None
        if answer:
            self.session.closing = True
            self.session.armed = False
            self.own(self.shutdown())
        else:
            self.quit_answered.set()

    async def shutdown(self):
        await self.session.stop(quitting=True)
        for wait in self.waits:
            if not wait.done():
                wait.set_result(None)
        tasks = self.tasks - {asyncio.current_task()}
        await asyncio.gather(*tasks, return_exceptions=True)
        self.exit(0)


def launch(paths, url):
    disk = QueueFile(paths.state)
    with disk.own():
        return YdlApp(paths, url, disk).run(mouse=True) or 0
