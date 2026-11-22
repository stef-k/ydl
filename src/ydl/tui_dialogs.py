"""Editable forms and choices that sit above the running terminal queue."""

import os
from pathlib import Path

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, Checkbox, DirectoryTree, Input, Label, RadioButton, RadioSet, Select,
                             Static, TabbedContent, TabPane, TextArea)

from . import config
from .models import YdlError
from .terminal import clean
from .tui_queue import Entry
from .tui_widgets import button, format_backend, quality_options, select


class Panel(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    heading = ""
    initial_focus = "#cancel"
    global_controls = True

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(clean(self.heading), classes="dialog-title", markup=False)
            with VerticalScroll(classes="dialog-body"):
                yield from self.body()
            with Horizontal(classes="dialog-controls"):
                yield from self.buttons()
            if self.global_controls:
                with Horizontal(classes="dialog-global"):
                    yield button("Stop queue", "modal-stop", "error")
                    yield button("Quit", "modal-quit")

    def buttons(self):
        yield button("Close", "cancel")

    def on_mount(self):
        self.ready = True
        self.limit_body()
        self.sync()
        self.query_one(self.initial_focus).focus()

    def on_resize(self):
        if getattr(self, "ready", False):
            self.limit_body()

    def limit_body(self):
        self.query_one(".dialog-body").styles.max_height = max(2, self.app.size.height - 7)

    def sync(self):
        if self.global_controls:
            stop = self.query_one("#modal-stop", Button)
            stop.display = self.app.session.busy
            stop.disabled = self.app.session.stopping
            stop.label = "Stopping…" if stop.disabled else "Stop queue"

    def action_cancel(self):
        self.dismiss()

    @on(Button.Pressed, "#cancel")
    def close_pressed(self):
        self.action_cancel()

    @on(Button.Pressed, "#modal-stop")
    def stop_pressed(self):
        self.app.action_stop()

    @on(Button.Pressed, "#modal-quit")
    def quit_pressed(self):
        self.app.action_quit()


class Confirm(Panel):
    def __init__(self, heading, question, accept="Confirm", cancel="Cancel", *, quits=False):
        super().__init__()
        self.heading, self.question = heading, question
        self.accept, self.cancel = accept, cancel
        self.global_controls = not quits

    def body(self):
        yield Static(clean(self.question), markup=False)

    def buttons(self):
        yield button(self.cancel, "cancel")
        yield button(self.accept, "confirm", "error" if self.accept in ("Remove", "Stop and quit") else "primary")

    @on(Button.Pressed, "#confirm")
    def yes(self):
        self.dismiss(True)


class RunnerPrompt(Panel):
    def __init__(self, decision):
        super().__init__()
        self.decision = decision
        self.scope = decision.kind == "scope"
        self.heading = "Playlist selection" if self.scope else "Download confirmation"
        default = next(index for index, (value, _) in enumerate(decision.choices) if value == decision.default)
        self.initial_focus = "#choices" if self.scope else f"#choice-{default}"

    def body(self):
        if not self.scope:
            yield Static(clean(self.decision.question), markup=False)
            return
        yield Static(f"Video ID  {clean(self.decision.video_id or '—')}\n"
                     f"Playlist ID  {clean(self.decision.playlist_id or '—')}", markup=False)
        with RadioSet(id="choices"):
            for value, label in self.decision.choices:
                yield RadioButton(label, value=value == self.decision.default)

    def buttons(self):
        yield button("Cancel", "cancel")
        if self.scope:
            yield button("Confirm", "scope-confirm", "primary")
        else:
            for i, (value, label) in enumerate(self.decision.choices):
                yield button(label, f"choice-{i}", "primary" if value == self.decision.default else "default")

    @on(Button.Pressed, "#scope-confirm")
    def scope_confirmed(self):
        index = self.query_one(RadioSet).pressed_index
        self.dismiss(self.decision.choices[index][0])

    @on(Button.Pressed)
    def choice_pressed(self, event):
        identifier = event.button.id or ""
        if identifier.startswith("choice-"):
            self.dismiss(self.decision.choices[int(identifier.removeprefix("choice-"))][0])


class AddURLs(Panel):
    heading = "Add URLs"
    initial_focus = "#batch-draft"

    def __init__(self, draft, options):
        super().__init__()
        self.original, self.options = draft, options
        self.saving = False
        self.recovery_draft = None

    def body(self):
        scope, quality, keep_video, output = self.options
        yield Static(f"{scope.capitalize()} · MP3 {quality} · Keep video {'on' if keep_video else 'off'}\n"
                     f"Save to {clean(output)}", markup=False)
        yield TextArea(self.original, id="batch-draft", placeholder="Paste YouTube URLs · one per line",
                       compact=True, soft_wrap=False)
        yield Static("", id="add-error", markup=False)
        yield Static("", id="add-storage", markup=False)

    def buttons(self):
        yield button("Cancel", "cancel")
        yield button("Add", "submit-add", "primary")
        yield button("Recover queue", "add-recover", "warning")

    def sync(self):
        super().sync()
        session = self.app.session
        error = session.index.error
        self.query_one("#add-storage", Static).update(clean(error))
        recover = self.query_one("#add-recover", Button)
        recover.display = bool(error)
        recover.disabled = not session.can_recover
        self.query_one("#submit-add", Button).disabled = self.saving or not session.index.editable or bool(session.backend.config_error)
        self.query_one(TextArea).read_only = self.saving or self.recovery_draft is not None

    def action_cancel(self):
        if not self.saving:
            self.dismiss(self.query_one(TextArea).text)

    @on(Button.Pressed, "#submit-add")
    def submit(self):
        if not self.saving:
            self.app.own(self.save_draft())

    @on(Button.Pressed, "#add-recover")
    def recover(self):
        self.app.own(self.app.recover_queue())

    def show_error(self, message):
        self.query_one("#add-error", Static).update(message)
        self.query_one(TextArea).focus()

    async def parse_draft(self, text):
        accepted, rejected = [], []
        index = self.app.session.index
        seen = {row.url for row in index.rows if index.view(row.id).status == "Pending"}
        for line_number, raw in enumerate(text.splitlines(), 1):
            if not raw.strip() or self.app.session.closing:
                continue
            try:
                entry = Entry.from_url(raw.strip(), *self.options)
            except YdlError as exc:
                rejected.append((line_number, raw, str(exc)))
                continue
            if entry.url in seen:
                dialog = Confirm("Duplicate URL", f"{entry.url} is already pending. Add another entry?", "Add duplicate")
                if not await self.app.ask(dialog):
                    rejected.append((line_number, raw, "Duplicate was not added."))
                    continue
            seen.add(entry.url)
            accepted.append((line_number, entry))
        return accepted, rejected

    async def save_draft(self):
        editor = self.query_one(TextArea)
        text = editor.text
        if not text.strip():
            self.show_error("Enter at least one YouTube URL")
            return
        self.saving = True
        self.sync()
        try:
            accepted, rejected = await self.parse_draft(text)
            if self.app.session.closing:
                return
            if accepted and not await self.commit(text, accepted):
                return
            if not rejected:
                self.dismiss("")
                return
            editor.load_text("\n".join(raw for _, raw, _ in rejected))
            self.show_error("\n".join(f"Line {number} · {reason}" for number, _, reason in rejected))
        finally:
            self.saving = False
            self.sync()

    async def commit(self, text, accepted):
        index = self.app.session.index
        if await index.edit(lambda rows: [*rows, *(entry for _, entry in accepted)]):
            index.notice = f"Added {len(accepted)} URL{'s' if len(accepted) != 1 else ''}."
            return True
        if index.error:
            self.recovery_draft = text, accepted
            self.app.recovery_draft = self.recovery_draft
            self.app.refresh_views()
        self.show_error(clean(index.error or index.notice or "The queue cannot accept changes right now."))
        return False

    def recovered(self, draft):
        self.recovery_draft = None
        editor = self.query_one(TextArea)
        editor.load_text(draft)
        self.show_error("")
        self.sync()


class Folders(DirectoryTree):
    def filter_paths(self, paths):
        return (path for path in paths if path.is_dir())

    def action_select_cursor(self):
        self.action_toggle_node()
        super().action_select_cursor()


def readable_folder(path):
    with os.scandir(path):
        return os.access(path, os.X_OK)


def browse_root(value):
    try:
        path = config.output_path(value)
    except (OSError, YdlError):
        return Path.home()
    for folder in (path, *path.parents):
        try:
            if readable_folder(folder):
                return folder
        except OSError:
            continue
    return Path.home()


class Browse(Panel):
    heading = "Choose output folder"
    initial_focus = "#folders"

    def __init__(self, value):
        super().__init__()
        self.root = browse_root(value)

    def body(self):
        tree = Folders(self.root, id="folders")
        tree.auto_expand = False
        yield tree
        yield Static("", id="browse-error", markup=False)

    def buttons(self):
        yield button("Parent folder", "parent")
        yield button("Cancel", "cancel")
        yield button("Choose folder", "choose", "primary")

    @on(Button.Pressed, "#parent")
    def parent_folder(self):
        tree = self.query_one(Folders)
        tree.path = tree.path.parent
        tree.focus()

    @on(Button.Pressed, "#choose")
    def choose(self):
        tree = self.query_one(Folders)
        node = tree.cursor_node
        path = node.data.path if node and node.data else tree.path
        try:
            if not readable_folder(path):
                raise OSError("Traversal is not permitted")
        except OSError as exc:
            self.query_one("#browse-error", Static).update(clean(f"Cannot open {path}: {exc} · Enter the path directly."))
            return
        self.dismiss(str(path))


class SettingsPanel(Panel):
    heading = "Settings"
    initial_focus = "#settings-output"

    def __init__(self, prefs):
        super().__init__()
        self.prefs = prefs
        self.saving = False

    def body(self):
        prefs = self.prefs
        with TabbedContent(id="settings-tabs"):
            with TabPane("General", id="general"):
                yield Label("Output directory")
                with Horizontal(classes="form-line"):
                    yield Input(prefs.output, id="settings-output", compact=True)
                    yield button("Browse", "settings-browse")
                yield Label("MP3 quality")
                yield select(quality_options(prefs.quality), prefs.quality, "settings-quality")
                yield Checkbox("Keep original video by default", prefs.keep_video, id="settings-video", compact=True)
            with TabPane("Backend", id="backend-settings"):
                yield Checkbox("Auto-update backend (yt-dlp)", prefs.auto_update, id="settings-update", compact=True)
                yield Label("Update channel")
                yield select([("Nightly", "nightly"), ("Stable", "stable")], prefs.channel, "settings-channel")
                yield Static("", id="settings-backend-status", markup=False)
                yield button("Retry backend", "settings-retry-backend")
        yield Static("", id="settings-error", markup=False)

    def buttons(self):
        yield button("Cancel", "cancel")
        yield button("Save Settings", "save-settings", "primary")

    def sync(self):
        super().sync()
        session = self.app.session
        retry = self.query_one("#settings-retry-backend", Button)
        retry.display = bool(session.backend_retry_visible)
        retry.disabled = not session.can_retry_backend
        status = format_backend(session.backend.status)
        self.query_one("#settings-backend-status", Static).update(clean(f"{status}\n{session.backend.status.detail}"))
        self.query_one("#save-settings", Button).disabled = self.saving

    def action_cancel(self):
        if not self.saving:
            self.dismiss()

    @on(Button.Pressed, "#settings-browse")
    def browse(self):
        self.app.browse(self.query_one("#settings-output", Input))

    @on(Button.Pressed, "#settings-retry-backend")
    def retry_backend(self):
        self.app.own(self.app.session.retry_backend())

    @on(Button.Pressed, "#save-settings")
    def save(self):
        if not self.saving:
            self.app.own(self.save_values())

    async def save_values(self):
        self.saving = True
        self.sync()
        values = dict(output=self.query_one("#settings-output", Input).value,
                      quality=self.query_one("#settings-quality", Select).value,
                      keep_video=self.query_one("#settings-video", Checkbox).value,
                      auto_update=self.query_one("#settings-update", Checkbox).value,
                      channel=self.query_one("#settings-channel", Select).value)
        try:
            await self.app.session.backend.save(values)
        except (OSError, YdlError) as exc:
            self.query_one("#settings-error", Static).update(clean(str(exc)))
        else:
            self.app.close_when_current(self)
        finally:
            self.saving = False
            self.sync()


class HelpPanel(Panel):
    heading = "Help"

    def body(self):
        yield Static("Tab / Shift+Tab   Move focus\n"
                     "Arrow keys        Navigate the focused control\n"
                     "Enter             Activate a choice, open Details, or insert a URL line\n"
                     "Escape            Cancel or close the current view\n"
                     "A                 Add URLs\n"
                     "R / Shift+R       Retry selected entry / Retry failed\n"
                     "D                 Job Details\n"
                     "S                 Settings\n"
                     "F2                Queue row actions\n"
                     "Delete            Remove selected row\n"
                     "C                 Clear completed\n"
                     "Ctrl+C            Stop queue\n"
                     "Ctrl+Q            Quit\n"
                     "?                 Help\n\n"
                     "Use the terminal’s paste command to paste URLs, one per line.\n"
                     "Keep the output directory in place during a download.", markup=False)
