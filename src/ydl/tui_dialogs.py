"""Editable forms and choices that sit above the running terminal queue."""

import os
from pathlib import Path

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, Checkbox, DirectoryTree, Input, Label, RadioButton, RadioSet, Select,
                             Static, TabbedContent, TabPane, Tabs)

from . import config
from .models import YdlError
from .terminal import clean, clean_lines
from .tui_widgets import FormError, button, format_backend, quality_options, select


class Panel(ModalScreen):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]
    heading = ""
    initial_focus = "#cancel"
    show_queue_stop = True
    ready = False

    def compose(self):
        with Vertical(classes="dialog"):
            yield Static(clean(self.heading), classes="dialog-title", markup=False)
            with VerticalScroll(classes="dialog-body"):
                yield from self.body()
            with Horizontal(classes="dialog-controls"):
                yield from self.buttons()
            if self.show_queue_stop:
                with Horizontal(classes="dialog-global"):
                    yield button("Stop queue", "modal-stop", "error")

    def buttons(self):
        yield button("Close", "cancel")

    def on_mount(self):
        self.ready = True
        self.limit_body()
        self.sync()
        self.query_one(self.initial_focus).focus()

    def on_resize(self):
        if self.ready:
            self.limit_body()

    def limit_body(self):
        self.query_one(".dialog-body").styles.max_height = max(2, self.app.size.height - 7)

    def sync(self):
        if self.show_queue_stop:
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


class Confirm(Panel):
    def __init__(self, heading, question, accept="Confirm", cancel="Cancel", *, quits=False):
        super().__init__()
        self.heading, self.question = heading, question
        self.accept, self.cancel = accept, cancel
        self.show_queue_stop = not quits

    def body(self):
        yield Static(clean(self.question), markup=False)

    def buttons(self):
        yield button(self.cancel, "cancel")
        yield button(self.accept, "confirm", "error" if self.accept in ("Remove", "Stop and quit") else "primary")

    @on(Button.Pressed, "#confirm")
    def yes(self):
        self.dismiss(True)


class RunnerPrompt(Panel):
    BINDINGS = [Binding("enter", "confirm_focused_scope", show=False, priority=True)]

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
                     f"Playlist ID  {clean(self.decision.playlist_id or '—')}", id="scope-ids", markup=False)
        yield Label("What do you want to download?", id="scope-question")
        with RadioSet(id="choices", compact=True):
            for value, label in self.decision.choices:
                yield RadioButton(label, value=value == self.decision.default, compact=True)

    def buttons(self):
        yield button("Cancel", "cancel")
        if self.scope:
            yield button("Confirm", "scope-confirm", "primary")
        else:
            for i, (value, label) in enumerate(self.decision.choices):
                yield button(label, f"choice-{i}", "primary" if value == self.decision.default else "default")

    @on(Button.Pressed, "#scope-confirm")
    def scope_confirmed(self):
        self.confirm_scope(self.query_one(RadioSet).pressed_index)

    def check_action(self, action, parameters):
        if action == "confirm_focused_scope":
            return self.scope and isinstance(self.focused, RadioSet)
        return super().check_action(action, parameters)

    def action_confirm_focused_scope(self):
        focused_option = self.query_one("RadioButton.-selected", RadioButton)
        self.confirm_scope(list(self.query(RadioButton)).index(focused_option))

    def confirm_scope(self, index):
        self.dismiss(self.decision.choices[index][0])

    @on(Button.Pressed)
    def choice_pressed(self, event):
        identifier = event.button.id or ""
        if identifier.startswith("choice-"):
            self.dismiss(self.decision.choices[int(identifier.removeprefix("choice-"))][0])


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
        yield FormError("browse-error")

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
            self.query_one("#browse-error", FormError).set_message(
                clean(f"Cannot open {path}: {exc} · Enter the path directly."))
            return
        self.dismiss(str(path))


class DialogTabs(TabbedContent):
    def on_key(self, event):
        if event.key not in ("up", "down") or not self.screen.has_class("-wide"):
            return
        tabs = self.query_one(Tabs)
        if tabs.has_focus:
            event.stop()
            event.prevent_default()
            if event.key == "up":
                tabs.action_previous_tab()
            else:
                tabs.action_next_tab()


class SettingsPanel(Panel):
    heading = "Settings"
    initial_focus = "#settings-output"

    def __init__(self, prefs):
        super().__init__()
        self.prefs = prefs
        self.saving = False

    def body(self):
        prefs = self.prefs
        with DialogTabs(id="settings-tabs"):
            with TabPane("General", id="general"):
                with Horizontal(classes="setting-row"):
                    yield Label("Output directory", classes="setting-label")
                    yield Input(prefs.output, id="settings-output", compact=True)
                    yield button("Browse", "settings-browse")
                with Horizontal(classes="setting-row"):
                    yield Label("MP3 quality", classes="setting-label")
                    yield select(quality_options(prefs.quality), prefs.quality, "settings-quality")
                yield Checkbox("Keep original video by default", prefs.keep_video, id="settings-video", compact=True)
                yield Checkbox("Remove from queue on success", prefs.remove_on_success,
                               id="settings-remove-on-success", compact=True)
            with TabPane("Backend", id="backend-settings"):
                yield Checkbox("Auto-update backend (yt-dlp)", prefs.auto_update, id="settings-update", compact=True)
                with Horizontal(classes="setting-row"):
                    yield Label("Update channel", classes="setting-label")
                    yield select([("Nightly", "nightly"), ("Stable", "stable")], prefs.channel, "settings-channel")
                yield Static("", id="settings-backend-status", markup=False)
                yield button("Retry backend", "settings-retry-backend")
        yield FormError("settings-error")

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
        output = self.query_one("#settings-output", Input)
        values = dict(output=output.value,
                      quality=self.query_one("#settings-quality", Select).value,
                      keep_video=self.query_one("#settings-video", Checkbox).value,
                      remove_on_success=self.query_one("#settings-remove-on-success", Checkbox).value,
                      auto_update=self.query_one("#settings-update", Checkbox).value,
                      channel=self.query_one("#settings-channel", Select).value)
        try:
            await self.app.session.backend.save(values)
        except (OSError, YdlError) as exc:
            self.query_one("#settings-error", FormError).set_message(clean_lines(exc))
            if isinstance(exc, config.InvalidOutput):
                self.query_one(DialogTabs).active = "general"
                self.call_after_refresh(self.set_focus, output)
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
                     "a                 Add to Queue (outside text fields)\n"
                     "R / Shift+R       Retry selected entry / Retry failed\n"
                     "D                 Job Details\n"
                     "S                 Settings\n"
                     "F2                Queue row actions\n"
                     "Delete            Remove selected row\n"
                     "C                 Clear completed\n"
                     "Ctrl+C            Stop queue\n"
                     "Ctrl+Q            Quit from the main screen\n"
                     "?                 Help\n\n"
                     "Use the terminal’s paste command to paste URLs, one per line.\n"
                     "Keep the output directory in place during a download.", markup=False)
