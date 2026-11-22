# ydl — Terminal UI (TUI) Design Specification

**Status:** First-release implementation handoff\
**Target:** Linux terminal, Python 3.11+\
**Builds on:** [`ydl-cli-spec.md`](ydl-cli-spec.md) and the existing implementation in [`stef-k/ydl`](https://github.com/stef-k/ydl)\
**Scope:** Add an optional interactive frontend, not a new downloader.

## 1. Objective

Make `ydl` convenient for people who prefer to paste one or several YouTube URLs, review the defaults, download them **sequentially**, watch progress, and retry failures without memorizing flags. **The queue must survive normal exits, restarts, and unexpected process termination.** Preserve the existing scripted command-line workflow exactly.

**Success means:** A user can start `ydl tui`, paste one or many links, enqueue them, and start a **persistent serial queue** that produces MP3 files with the same quality, safety, and recovery guarantees as `ydl URL`. On reopening `ydl tui`, the URL list, order, chosen options, and unfinished work are restored without starting downloads automatically. Each queued URL is a distinct existing-engine job, and any playlist is processed serially inside that job.

The TUI is a **presentation and interaction layer** on the existing asynchronous engine. It must not duplicate yt-dlp invocation, media conversion, update management, job state, or filesystem publishing logic.

## 2. Observed implementation baseline

The current repository already provides useful frontend boundaries:

- `src/ydl/runner.py`: async `download(Download(...), ..., emit=..., decide=...)` and `retry(...)` entry points. Each is responsible for its operation's backend readiness, workflow, and job lifecycle.
- `src/ydl/models.py`: typed `Event`, `Decision`, `Result`, and `Settings` objects. `emit(Event)` is synchronous; `decide(Decision)` returns an awaitable choice.
- `src/ydl/jobs.py`: persistent job records, resumption, per-job locks, and verified outputs.
- `src/ydl/process.py`: async subprocess execution and cancellation of subprocess groups.
- `src/ydl/terminal.py`: current line-based presentation, which remains the default for ordinary CLI use.
- `src/ydl/config.py`: settings and XDG paths; defaults remain the single source of truth.
- `src/ydl/cli.py`: `ydl` argument parser and dispatch, currently supporting URL, retry, doctor, config, and update commands.
- `pyproject.toml`: currently has no runtime dependencies; preserving this for normal CLI installations is desirable.

**Architectural constraint:** Reuse these contracts. If a missing read-only query or event is needed, extend the shared layer narrowly; do not create a separate TUI-only downloading path.

## 3. Entry points and installation

- `ydl URL [flags]` remains unchanged, including its prompts, exit codes, and non-interactive behavior.
- `ydl tui` opens an interactive full-screen terminal interface.
- `ydl tui URL` optionally pre-fills the URL input but **does not enqueue or download automatically**.
- `ydl tui --help` describes the TUI command. `ydl --help`, `ydl retry`, `ydl doctor`, `ydl update`, and `ydl config` continue to work.
- `ydl` without arguments retains its current behavior; do not silently change it to launch the TUI.
- Require both stdin and stdout to be TTYs and `TERM` to be set to a value other than `dumb`. Fail with a plain-text hint to use `ydl URL` before writing escape sequences. Normal Quit exits `0`, even if queue entries failed. Invalid arguments use the parser's exit `2`.

**Framework choice:** Use [Textual](https://textual.textualize.io/) (MIT) as an **optional** dependency. It supplies mouse and keyboard input, async workers, widgets, layout, and terminal-size handling. Do not create a homegrown curses framework or introduce Qt, GTK, Flatpak, Electron, or a persistent service.

Suggested packaging:

```toml
[project.optional-dependencies]
tui = ["textual"]  # Constrain to a tested compatible range during implementation.
```

From the checkout, install with `pipx install '.[tui]'`. An existing pipx installation can be replaced with `pipx install --force '.[tui]'` so both routes use the same tested dependency range. Parse `ydl tui --help` before checking terminal capabilities or importing Textual. For a missing optional dependency or unsupported terminal, exit `1` with a plain-text remedy. The base CLI continues to work without Textual. Developer-only Textual tooling stays outside the runtime extra.

## 4. Layout and interaction

Use one main screen with a fixed dark Textual theme. Show the app name, package version when available, backend status, input form, queue, active transfer and action footer. Put Settings, Add URLs, Browse, Job Details, Recent jobs and Help in separate views opened on demand. Use terminal cells and readable labels. The layout sketch and control rules in this document are the implementation contract.

At 80 columns by 24 rows, keep the form, queue, active transfer, Start/Resume or Stop, and Help reachable by mouse and keyboard. The compact queue shows position, title, status and the row-action button. At 100 columns and above, add Type, Track progress and Output columns. A smaller terminal can scroll the form and queue. Keep all secondary columns available in Job Details. When a secondary view closes, restore focus to its opener or the queue if the opener is gone.

```text
┌─ ydl ─────────────────────────── Backend: up to date ───────────┐
│ URLs [ Paste one or multiple links here ]  [ Add to queue ]     │
│ Scope: [ Auto ▼ ]  Quality: [ VBR 0 ▼ ]  Keep video: [ ]        │
│ Save to: [ ~/Downloads/ydl                                ]    │
│ [ Start queue / Stop queue ] [ Retry failed ] [ Settings ]      │
├─ Queue — 1 active / 2 pending / 1 complete / 0 failed ──────────┤
│ ▶ 1. Playlist A    3/24  Downloading  56%  ETA 00:18            │
│   2. Video B       Pending                                    │
│   3. Video C       Pending                                    │
│ ✓ 4. Video D       Complete                                   │
├─ Active item ───────────────────────────────────────────────────┤
│ Track title   [████████░░░░░░░░░░] 56%   2.1 MiB/s             │
└─ Tab navigate · Enter select · Ctrl+C stop · Ctrl+Q quit ──────┘
```

Use text labels for status even when also using color or a symbol. Show help on demand instead of a permanent features panel. When an existing queue is restored, show a notice with its Pending and Interrupted counts after reconciliation. The next queue action or error replaces that notice.

### Serial URL queue (required)

- Use the same Add URLs dialog for one URL or newline-separated URLs. Trim blank lines. Validate each line with `media.parse_url` and validate its selected scope with `media.validate_scope`. Commit the accepted entries together and keep rejected lines editable with their errors. Check duplicates against pending rows and the accepted lines of the current batch.
- Each accepted URL becomes a separate queue entry. Save its canonical URL, requested scope, quality, video-retention choice, and destination. Expand `~` and resolve the output path at enqueue time, including any symlinks present then. Save the resulting absolute path as the destination for that row. Map the stored `auto` scope to `None` at the runner boundary. The current configuration supplies defaults for new entries. Form edits leave saved entries unchanged.
- Show the durable row states below and transient stages such as Checking, Downloading, and Converting. Use the queue actions table for enablement and placement.
- Execute entries **strictly one at a time**: `await runner.download(...)` or `await runner.retry(...)` must finish (including conversion, verification, and publication) before the next starts. One queued playlist itself runs items serially using the existing runner. No parallel URL jobs, worker pool, or additional yt-dlp instances for media downloads.
- **Start/Resume queue** processes Pending and Interrupted rows in their current displayed order. **Retry failed** makes one pass over the Failed row IDs present when pressed, in queue order. It leaves Pending and Interrupted rows for Start/Resume. Each action attempts a row at most once and advances after saving the settled result.
- Select the next queue row and save it as Active in one serialized transaction after backend preparation. Start/Resume reads the current order and skips already attempted rows. Retry failed and selected-entry retry keep their original targets and skip any target that is gone or no longer eligible. An Add, removal, or move committed before this transaction affects its selection. Once Active is durable, the row is ineligible for removal or movement until settlement. If no row is eligible, end the action.
- A failure stores the core diagnostic and exposes the job ID and log path when available. A refusal or dismissal follows the decision outcomes below. A recoverable entry failure leaves later entries eligible to run during the same action.
- **Stop queue** and Ctrl+C close the scheduling gate immediately, cancel the active operation, and await subprocess cleanup. Check the gate before saving Active and again before invoking the runner. If scheduling is disarmed between those steps, restore the previous hint, buffering it during storage recovery. If Stop cancels an invoked attempt, save Interrupted when there is a plan or the plan write is uncertain. Save Pending when the runner confirms it stopped before creating a plan. Preserve any settled result already received, along with saved plans, partials and finished files. Enable Start/Resume again after cleanup and a successful queue save, or after storage recovery succeeds.
- A brief queue summary reports count of queued URLs, completed URLs, failed URLs, and active playlist item position. Do not equate a playlist of 100 tracks with 100 queued URLs; distinguish URL queue progress from the existing per-playlist item progress.
- Ask before adding an exact canonical duplicate of a pending or accepted batch URL. Rejected duplicates stay out of the transaction. The core retains its filename collision rules for distinct entries that resolve to the same output.

### Queue actions and selection

A queue action is busy while waiting on backend preparation, running, or settling after Stop. The same serial-worker gate applies to Start, both retry modes, and retry from Recent jobs. Select the first row when opening a nonempty queue or adding to an empty one. Keep selection by row ID across refreshes and moves. When the selected row is removed, select the next row at the same position or the preceding row if there is no next row.

Disable Start/Resume, Retry failed and queue-linked selected retry during startup reconciliation, recovery reconciliation, or an explicit queue Recheck. Enable them only after all rows in that pass have a classification and required queue writes have succeeded. Pressing a shortcut while disabled does not arm an action. Retry failed snapshots the displayed Failed rows in order when the user activates it after verification. If B has verified as Failed while A is still Verifying, the action stays disabled. If both verify as Failed, the next activation attempts A then B. Verification in the separate Recent jobs view gates only retry of its selected external job, subject to the shared serial and queue storage gates.

| Control | Enabled and result |
| --- | --- |
| **Start queue / Resume queue** | Primary action when idle and at least one Pending or Interrupted row is eligible. Use Resume if any eligible row is Interrupted; otherwise use Start. Run the Start/Resume action defined above. |
| **Stop queue** | Replaces the primary action while an action is armed or running. Use the existing stop contract. While cleanup is in progress, show **Stopping…** and disable further starts and retries. |
| **Retry failed** | Main-screen batch action, enabled while idle when at least one Failed row exists. Run the Failed rows once in queue order. |
| **Retry selected entry** | Enabled while idle for a selected Failed row or an Interrupted row with a saved plan. Run only that row. Apply the same plan selection and durable transitions as a queue run. |
| **Details** | Open the selected row's Job Details even while the queue is running. Reading details does not claim or change the job. |
| **Move up / Move down** | Swap a selected Pending row with the nearest Pending row in that direction. Other rows keep their positions. Disable the action when no such row exists. |
| **Remove selected / Clear completed** | While busy, only Pending rows may be removed. While idle, any inactive row can be removed and completed rows can be cleared together. Confirmation states that the action removes queue entries only and defaults to Cancel. Recheck eligibility before committing the change. Preserve job plans, media, logs and partials. |
| **Recheck** | Available for an inactive queue row while idle, with storage healthy and no reconciliation in flight. Reconcile that row without starting work. |
| **Retry backend** | Show beside backend status and in Settings after an operational preference read fails, while a check is pending after configuration repair, or after a backend check fails without a usable fallback. Enable while idle, with queue storage healthy and without another check in flight. Re-read preferences and run the check without arming downloads. |
| **Recover queue** | Show in the queue storage error notice. Enable after existing work has settled and while no recovery is in progress. Run the in-place storage recovery flow and leave scheduling idle. |

Open the selected row's `⋮` menu from its button or `F2` on the queue table. Put Details, Retry selected entry, Move up, Move down, Remove selected, and Recheck in this menu. Display their unavailable states as disabled. Enqueued options are read-only. To change pending options, remove the pending row and add it again with the new choices.

### Persistent queue and restart recovery (required for the first TUI release)

**User-facing contract.** A saved queue survives application exits, terminal closure, a Linux reboot, and unexpected interruption. It keeps pending URLs, their order and options, and the association with started jobs. Opening `ydl tui` restores the queue and leaves it idle until the user explicitly starts or retries work. A job plan is the existing core record of the resolved destination, settings and ordered tracks. It also stores the receipts used to verify finished outputs.

**Storage and ownership**

- Use a schema-versioned JSON document named `tui-queue.json` under the `Paths.state` directory. The document stores ordered rows with unique UUID4 `id` values in 32-character lowercase hexadecimal form. Each ID is also reserved as the core job ID. Store the canonical URL, requested scope, quality, retention flag, resolved output directory, a lifecycle hint (`pending`, `active`, `complete`, `failed`, `failed_before_plan`, `cancelled`, `interrupted`), and any entry diagnostic. New rows start `pending`. Both failure hints display as Failed when eligible. `failed_before_plan` means the runner confirmed the attempt failed before creating a plan. `JobStore` owns the plans and receipts. Progress, credentials and fragments stay outside the queue file.
- Canonicalize through the core URL parser. Keep the normalized YouTube host and relevant video and playlist IDs, including both IDs when present. Drop tracking parameters. Running a saved row must allow the same scope choices as its original URL.
- Take a nonblocking exclusive POSIX lock on a dedicated queue lock file before restoring the queue or starting backend work. Hold it for the session, including shutdown. A second TUI exits `1` with a plain-text message identifying the busy queue, including when a supervisor is still cleaning up work from a crashed session. CLI downloads continue to use independent per-job ownership.
- Sync each accepted queue mutation as one transaction before reporting success. Keep disk sync off the UI loop. Write a temporary file beside the queue file, `fsync` the file, replace the queue file atomically, and `fsync` the directory, including any newly created parent entries needed to reach it. Save Active before invoking the runner and save the settled status before advancing to another row. Progress redraws do not write the queue file.
- A failure before replacement rejects an ordinary edit and keeps the previous durable queue and the affected draft. A failure after replacement but before directory sync enters storage recovery. A failed Active, terminal-result, or reconciliation save also enters recovery. Stop and Quit remain available during storage errors.

**In-place storage recovery**

1. Show the queue path, the save error, and **Recover queue**, including in the Add dialog if it is open. Distinguish a rejected save from a save whose durability is uncertain. Disarm scheduling and disable queue edits, new backend checks and new runner attempts. An already invoked attempt can settle using its own job store, including if the user accepts a pending runner decision. Stop remains available. Preserve the draft and its reserved IDs in memory. Buffer the settled status and diagnostic by row ID while recovery is active. A declined playlist buffers Cancelled with **Playlist download declined**. If the Active save failed before runner invocation, buffer the previous hint so in-place recovery can restore it.
2. Once owned work settles, **Recover queue** reopens the queue file under the existing session lock. Validate the snapshot and `fsync` the file and its containing directory, including any parent entries required to make the path durable. A successful read alone does not admit the snapshot. If validation or sync fails, show the error and stay in recovery. An in-place recovery must leave a missing or invalid file untouched.
3. After successful sync, use the recovered rows and order. Apply buffered results to matching row IDs and durably save their statuses and diagnostics before reconciliation. Keep buffered results until this save succeeds. If a row is missing from the recovered snapshot, report its outcome without recreating the row. Reconcile against the job store and save required promotions. Recover queue never replays an Add, removal or move. Remove a draft line only when its reserved ID is present in the recovered queue. Enable queue controls only after all recovery saves and reconciliation succeed, and leave the queue idle.
4. Restarting validates and syncs the surviving queue before enabling changes. A missing queue file starts an empty index. Drafts and buffered results are session-only. Use the restore table when an attempt outcome was lost. Warn in the Quit confirmation when exiting would discard a draft or buffered result.

**Job handoff and process ownership**

- Add a validated optional preassigned ID to `runner.download()`. Ordinary CLI downloads keep automatic IDs. For a new queue attempt, the core must acquire the per-job lock before checking for a plan at that ID. Under the lock, durably create a new plan only if none exists. Treat an existing plan, including an unreadable one, as already occupied. Return a distinguishable existing-plan or busy outcome so the TUI can reconcile it. The core holds the lock through execution and cleanup.
- Keep the queue ID durable before invocation. The core saves the plan with file sync, atomic replacement and directory sync, including the entry for a newly created jobs directory, before starting media work. Persist verification receipts durably before publishing outputs. The core uses the job store to settle the attempt. The TUI saves its result separately after the attempt. A standalone `ydl retry` uses the job store and per-job lock, including when the queue needs repair.
- For ordinary operational failures, save `failed_before_plan` if the runner confirms it created no plan, and `failed` if it used or saved a plan. An initial plan write failure before replacement counts as `failed_before_plan`. A failed initial write after replacement leaves the plan file in place. Save Interrupted, show **Needs attention** with the plan path and error, and let Recheck validate and sync the surviving plan before offering Resume. Before using a queue-created plan for retry, the core validates and syncs it under the job lock. If this fails, the TUI preserves the previous hint and shows Needs attention. The CLI reports the path and reason and exits `1`.
- Dispatch a row with a reconciled saved plan through `runner.retry(id)`, using the original options and track order. Use `runner.download()` with the reserved ID only for a row eligible for new planning. If the saved plan disappears before the core loads it, return a distinct missing-plan outcome. Save the pre-attempt hint and use the diagnostic and recovery choices in the restore table. Skip the row for the rest of the action. If the original plan is restored, **Recheck** recovers the row's previous retry category.
- Use a short-lived Linux supervisor for each managed command tree. It observes loss of its parent through a lifetime pipe and terminates and reaps the tree on parent loss. Give it a duplicate of the job lock for job commands, including CLI retries, and a duplicate of the session queue lock for TUI commands. The supervisor receives those descriptors before it starts the managed command. Keep each lock held until the tree exits. Close the duplicate descriptors without explicitly unlocking the shared lock. The next TUI cannot open the queue or start any row while its predecessor's supervisor still owns it.
- Resume uses the same core job lock as the original attempt. If a separate CLI invocation owns a job when the TUI inspects it, show Needs attention with a reason that the job is busy. Neither frontend can take over that job until ownership is released. Normal Stop and Quit await cleanup before acknowledging completion.

**Restore behavior**

Restore the queue locally using the durability rules above. Show rows in saved order while integrity checks run asynchronously. Show Verifying while an integrity result is pending. Restore makes no YouTube requests. The separate backend startup check may contact the release service after the queue file has been admitted as durable, while integrity checks are still running.

Reconcile using the following rules in order. In both inspection views, take a nonblocking shared lock on the existing job lock file and hold it through plan loading, destination checks and output verification. This gives the verifier a consistent view while a CLI retry may be trying to claim the same job. CLI retry can report the job as busy while verification is in progress. If a plan has no lock file, show **Needs attention** with the missing-lock reason and suggest `ydl retry JOB_ID`, which can acquire the job lock through the core. Validate queue-created plans and sync the plan file and jobs directory under the lock before treating a plan as resumable. The core takes the exclusive lock and rechecks the plan on every attempt.

| Saved plan and verified outputs | Restored row | Explicit action |
| --- | --- | --- |
| Job is busy, an existing plan cannot be loaded or synced, the enqueued or saved destination is invalid or unreadable, or an occupied output has no matching verification receipt | Needs attention, with the reason | **Recheck** after the job releases ownership or the obstruction is addressed. |
| Every requested item has verified outputs, even if the TUI last saved Active or the core completion flag was still false | Complete | Clear completed to remove the row. No media work is needed. |
| Valid plan with unfinished items, and the last hint was `failed` or `failed_before_plan` | Failed | **Retry failed** uses `runner.retry(id)`. |
| Valid plan with unfinished items, including missing files from a previously Complete row | Interrupted | **Start/Resume queue** uses `runner.retry(id)`. |
| No plan, and the last hint was `active`, `interrupted`, `failed` or `complete` | Needs attention, with the expected job file path | Recheck after restoring the original plan, or use **Add** to start fresh as a separate entry with a new ID. The diagnostic tells the user both choices. |
| No plan, and the last hint was `failed_before_plan` | Failed | **Retry failed** uses `runner.download()` with the original ID and options. |
| No plan, and the last hint was `cancelled` | Cancelled | Remove the row or explicitly add the URL again. |
| No plan, and the last hint was `pending` | Pending | **Start queue** uses `runner.download()` with the original ID and options. |

Reconciliation saves Complete whenever all expected outputs verify, including after a CLI retry. When a valid plan exists, convert a `failed_before_plan` hint to `failed`, and a `pending` or `cancelled` hint to `interrupted`. Save those changes before enabling actions, even when an output or a destination needs attention. Other display classifications keep the previous hint. For example, a Complete row with a missing output displays Interrupted. Apply the changes by row ID in one serialized write, preserving other edits and skipping removed rows.

If new planning encounters an existing plan, reconcile using the pre-attempt hint, save the reconciled hint, and skip the row for the rest of this action. Retry the saved plan on the next explicit action if eligible. If dispatch finds the job busy, restore the pre-attempt hint with the busy diagnostic and skip the row for this action. Recheck after the owner exits.

On corrupt or unsupported queue JSON, leave the original file in place, show its path, and disable edits, backend startup and scheduling. Tell the user to restore a valid copy and use **Recover queue**, or move the file aside and reopen to start an empty queue. `ydl retry JOB_ID` remains available for intact job records.

**YouTube availability can change between sessions**

- A queued URL eligible for new planning is resolved when it is explicitly attempted. If YouTube reports the video or playlist unavailable, removed or private during planning, save `failed_before_plan` with the core diagnostic. Keep its URL, options and ID. Advance to the next eligible row. A later Retry failed can try the same entry again.
- A saved plan is retried by original video IDs and never rebuilt from the current playlist. If the playlist URL has disappeared, any original video IDs that still work can finish. Skip verified outputs even if their videos are gone. If an unfinished video has become unavailable, mark that item failed through the core, continue the remaining items, and report the URL as Failed if any requested item remains unfinished. Then continue the URL queue.
- Fully verified jobs require no YouTube availability check. To download the current membership of a changed playlist, explicitly add its URL as a new queue entry. The new entry uses the normal output collision rules. The old entry keeps its saved plan.

**Minimal serialized example (illustrative only):**

```json
{
  "schema_version": 1,
  "entries": [
    {
      "id": "9ce427d9b0214ef18d1e13a2ed69ec40",
      "url": "https://www.youtube.com/watch?v=vvpZOaQz6SM",
      "scope": "auto",
      "quality": "0",
      "keep_video": false,
      "output": "/home/example/Downloads/ydl",
      "status": "interrupted"
    }
  ]
}
```

Array order is queue order; the `id` is also the saved job ID once the runner creates the plan. This record does **not** duplicate playlist-item manifests or verification hashes.

### URL entry and download options

- Focus the compact multiline URL draft on startup. Terminal paste works in the draft and Add URLs dialog. Display a terminal paste hint and leave system-clipboard reads outside this interface. Enter inserts a newline in the editor.
- **Add to Queue** and the Add shortcut first validate the option fields and then open one Add URLs dialog for the entire draft. The dialog shows the selected options and has a multiline URL editor, **Add**, and **Cancel**. An empty submission shows **Enter at least one YouTube URL** and leaves the queue intact. Add commits the accepted lines together. Close the dialog and clear the draft when all lines were accepted. If some lines fail validation or duplicate confirmation, leave only the unadded lines in the dialog with their reasons. Cancel or Escape returns those unsubmitted lines to the main draft. Entries already saved stay in the queue.
- **Scope**: Auto (default), Single video, Entire playlist. Auto delegates to existing `runner.scope_for`; show a modal for video-plus-playlist URLs. Pure playlist URLs use playlist scope. Preserve current large/unknown-playlist confirmation.
- **Quality**: built-in default VBR `0`. Offer VBR `0`–`9` and common explicit bitrates (`192K`, `256K`, `320K`). Include a saved valid bitrate in the chooser even when it differs from these presets. Use `quality_value` to validate the selected value.
- **Keep video**: checkbox, default from saved preferences. Pass its selected value explicitly to the runner to avoid a duplicate yes/no prompt.
- **Destination**: editable path with default from preferences and a labeled **Browse** action. Use the existing path validator. The user can type a directory that has yet to be created.
- At dispatch, validate the saved absolute path as-is. Reject a symlink, non-directory, or access error in any component. The runner can create missing directories under the validated path. Queue-created plans store the enqueued base so either frontend can do the same checks on retry. A blocked destination gives **Needs attention** with the path and reason. Use **Recheck** after fixing it or add a new entry to choose another folder.
- Ask users to keep the output folder in place during a download. Record the base and resolved job directory identities when each is ready, using the device and inode. Check that the paths still reach those directories before publishing finished files and before reporting success. If a check detects a change, stop the attempt, keep the saved plan and existing files, and report the original path. Show **Needs attention** for a queued entry and keep the previous hint, using the restore rules to convert it if the attempt saved a plan. A CLI retry of a queue-created plan exits `1`. Recheck after putting the folder back. Commands already in flight use ordinary paths and may fail if the folder moves.
- If the URL or field is invalid, focus the affected control, show an inline actionable error, and leave the user-entered data intact.

### Directory chooser and Settings

- Use the same keyboard-accessible directory dialog for Browse in the main form and Settings. Start at the current field's resolved directory if it exists and is readable, otherwise use its nearest readable existing parent or the user's home directory. Use a Textual DirectoryTree restricted to directories. Enter expands or collapses a tree node. **Choose folder** copies the selected path into the field only. Cancel or Escape leaves the field unchanged. If a path cannot be opened, show the error and leave typed path entry available. The chooser does not create a directory or start work.
- Settings has two tabs. **General** edits the default output directory, MP3 quality and Keep original video. **Backend** edits the Auto-update backend checkbox and Nightly/Stable selector. It also shows the same backend status and Retry backend action as the main screen. Scope remains a per-entry choice. The first release has no Downloads, Queue or Appearance settings tabs.
- Open Settings from the gear or its shortcut. Initialize from the current shared configuration or the repair values defined under Backend startup and reuse. **Save Settings** validates the five preference values together and commits them in one atomic write through the shared config code. On failure, keep the editor open and display the error. Successful Save closes the view and updates the main form's destination, quality and retention defaults while preserving its URL draft and scope. Cancel or Escape discards the edits. Queue rows keep their snapshots.
- Settings can open while a job runs. Saving a channel or auto-update change invalidates readiness for the next operation and leaves the current job running. Retry backend is unavailable while a queue action or check is in progress.

### Decisions and confirmations

Render the core's `Decision` question, options and default in a modal. Enter activates the focused choice, initially the core default. Await the response in the app-owned serial worker. Always settle or cancel the pending wait before leaving the dialog.

- In Auto scope, the **Playlist selection** dialog offers **This video** and **Entire playlist**, with This video initially selected. The first scope dialog appears before metadata lookup. Show the available video and playlist IDs and omit media title, duration, artwork and item count from that dialog. Confirm passes the selected scope to the core. A separate size confirmation uses the count or unknown-size wording supplied by the core. Keep TUI runner decisions interactive.
- Choosing **No** to a playlist confirmation uses the core's declined result and saves Cancelled. Choosing **No** to Deno installation uses the core's missing-runtime failure and saves Failed. Both allow the next queue entry to run after settlement.
- Escape or Cancel in a runner dialog aborts only the current attempt. Return a distinct dismissal outcome to the scheduler. Save Cancelled if the attempt has no plan, or Interrupted if an existing plan is retained. Continue with the next eligible row, skipping the dismissed row for the rest of this action. A dismissed pre-plan dialog creates no job plan or transfer.
- Show a runner prompt above any open input, Settings, or details view, preserving its draft and focus. If Quit confirmation is already open, hold the pending runner prompt until Stay is selected. Stop or confirmed Quit dismisses runner prompts and settles their waits without committing drafts in the underlying views.
- Stop queue and Ctrl+C close the scheduling gate even while a runner dialog is visible. Dismiss the dialog, cancel and await the operation, and preserve a settled terminal result if one was already committed. Confirmed Quit uses the same stop path before exit. Declining Quit leaves the prior dialog or operation in place.

### Queue display and progress

- Before a plan exists, label rows using IDs available from the URL, such as `Video VIDEO_ID`, `Playlist PLAYLIST_ID`, or `Video VIDEO_ID + playlist PLAYLIST_ID`. Use the requested scope to label a forced single or playlist entry. Type is Video, Playlist, or Video + playlist for unresolved Auto scope. Show a playlist count only once the core provides the saved plan or playlist total. Adding and restoring rows never fetch titles, durations or artwork from YouTube.
- With a saved single-video plan, use the item title. With a saved playlist plan, use the saved destination folder name. Read those fields and final paths from the shared job inspection boundary. During execution, use Settings events for scope and destination and item events for the current track title and position. Type, output and known counts are available in Job Details even when their table columns are hidden.
- Label a percentage or bar **Track progress**. For a playlist, also show its `3/24` position separately. Keep speed and ETA scoped to the current transfer. Show indeterminate progress during an active stage with no reliable percentage. Clear transfer metrics as the active item or stage changes. Completed rows can show 100% after verification. Inactive unfinished or failed rows show a dash instead of a stale percentage.
- Show the actual MP3 filename when a single-video output is verified and the resolved folder for a planned playlist. For an unplanned row, show a dash in Output and the enqueued base directory in Info. On settlement show the core's success and failure counts, resolved destination when available, and actionable diagnostic on failure. Queue counts measure URL rows.

### Job Details

Open Job Details for the selected queue row or recent job, with the **Progress** tab selected. **Close** and Escape return focus to the opener. Keep the view tied to that entry when the table selection later changes or a different job starts.

| Tab | Contents |
| --- | --- |
| **Progress** | Row status, stage, current track and position, track percentage, speed and ETA when available, plus the final result after settlement. Use saved status and verified counts for an inactive job. |
| **Log** | Show the log path and at most the last 200 lines or 64 KiB of the existing log, whichever is smaller, as plain text. Indicate when older text is omitted. Load on entry and on **Refresh** in a read-only worker. Show **No log yet** for a missing file and an actionable error for a failed read. **Copy log path** requests terminal clipboard copy when a log path exists. Keep the path visible for manual copying. |
| **Info** | Show the reserved queue ID, core job ID when a plan exists, canonical URL when known, requested and resolved scope when known, MP3 quality, Keep video, enqueued base directory when known, resolved output, and latest diagnostic. Mark unavailable fields with a dash. |

The Details action is **Retry selected entry** for a queued row or **Retry selected job** for a recent job outside the queue. Apply the same eligibility and worker rules as their other entry points. Retry targets the whole saved job's unfinished work. There is no retry for an individual track within a playlist.

### Recent jobs and retry

Open **Recent jobs** from the footer. Show the 20 most recently created saved jobs, ordered newest first with job ID descending as the tie-breaker. For jobs without queue history, display Verifying, Complete after output verification, Incomplete, or Needs attention with the reason. Use the queue's Failed and Interrupted distinctions for linked rows and Active for work currently owned by this TUI. The core records do not need a new history field. **Retry selected job** is enabled while idle for an Incomplete external job and calls `runner.retry(id)` through the serial worker and backend gate. Linked jobs use the queue's selected-entry action. External jobs stay outside the queue index.

Share a read-only inspection boundary for queue reconciliation and the recent-jobs list. The query leaves job state untouched, uses no exclusive claim, and does not create a lock file. Queue reconciliation also performs the plan admission step described above. A malformed historical record is flagged without preventing fresh downloads.

Before checking receipts, use the same destination validation as `runner.retry()`. Queue-created plans use the path checks above. Ordinary CLI plans use the core's existing check for a real directory at the saved destination. An invalid or inaccessible path gives **Needs attention** even if receipts match. An absent directory counts as unfinished. Only the runner creates directories, under the job lock.

## 5. Mouse and keyboard behavior

### Mouse interaction

- Enable Textual's mouse input by default. On a terminal that sends mouse reports, every app action has a clickable control or menu entry, including Help, Recent jobs, Quit, row actions and confirmation choices. Terminals without mouse reporting retain the full keyboard workflow. Mouse and keyboard invoke the same action and eligibility rules.
- One click on a queue or recent-job row selects it. Double-click its body to open Details. Click the queue row's `⋮` button to select that row and open its action menu. A secondary click on that row opens the same menu when the terminal reports it. Use Move up and Move down in the menu to reorder pending entries.
- Click settings and detail tabs, dropdowns, checkboxes, and dialog options to select them. In Browse, click a directory row to select it and its disclosure control to expand or collapse it. A confirmation choice is committed by its labeled confirmation button. All secondary views and dialogs have a clickable Close or Cancel action.
- Support wheel scrolling in the queue, forms, menus, directory chooser, Recent jobs, Help and log view wherever content overflows. Scrollbar thumbs can be dragged wherever scrollbars are displayed. Scrolling leaves the selected row unchanged. Labels and actions remain available without hovering.
- Keep a clickable **Quit** in the main action area. Any secondary view or dialog that covers that area exposes the same Quit action and, while the queue is armed or running, **Stop queue**. These controls use the shared stop and confirmation flow below, including when a runner prompt is open. Mouse Stop from a runner prompt stops the queue. Clicking Cancel on that prompt dismisses only the current attempt.
- Clicking an editor focuses it for input. Use the terminal's normal paste action to bring external text into the field. Secondary clicks on editable fields are not used for queue actions.

### Keyboard shortcuts

Show a Help button in the footer and list these bindings in Help. Suppress the main-screen shortcuts when a text editor or modal has focus. Tab, Escape, Ctrl+C and Ctrl+Q still work within their defined contexts.

| Key | Action |
| --- | --- |
| `Tab` / `Shift+Tab` | Move focus forwards or backwards. Arrow keys navigate the focused queue, menu, tab list, tree or choice list. |
| `Enter` | Open Details on a queue row. In an editor or chooser, use the behavior defined for that field. Otherwise activate the focused control or dialog choice. |
| `Escape` | Close the topmost dialog or menu as Cancel. The runner-dialog dismissal rules apply to a runner prompt. |
| `a` | Open Add URLs. |
| `r` / `Shift+R` | Retry selected entry / Retry failed. |
| `d` | Open Details for the selected row. |
| `s` | Open Settings. |
| `F2` | Open the selected queue row's action menu. |
| `Delete` | Request removal of the selected queue row. Inside a text editor, retain normal text deletion. |
| `c` | Request Clear completed. |
| `Ctrl+C` | Stop a running queue or cancel its armed Start/Retry request. It has no queue effect when idle. |
| `Ctrl+Q` | Quit. Confirm when an operation is active, a queue action is armed, or quitting storage recovery would discard a draft or buffered result. Offer **Stay** as the default and **Stop and quit** when work is active or armed, otherwise **Quit**. Cancel and await all owned workers before exit. |
| `?` | Open Help. |

## 6. Architecture and lifecycle

Suggested minimal new files:

```text
src/ydl/tui.py           # Textual application, dialogs, widgets, event adapter
src/ydl/tui.tcss         # Optional small stylesheet if inline styling becomes noisy
tests/test_tui.py        # Textual Pilot-based behavior tests with fake runner
src/ydl/tui_queue.py     # minimal versioned queue, atomic file writes, advisory lock
```

Only create additional modules if tests show a real need. Keep `src/ydl/terminal.py` as the existing basic terminal frontend.

### Reuse existing async boundaries

- The TUI launches the same `runner.download()` and `runner.retry()` coroutines from **one serial queue worker**; `await` completion of each URL before advancing. Never nest `asyncio.run()` inside the UI loop. The queue orchestrator must not reimplement download, playlist, or retry logic.
- The TUI supplies `emit(event)` to update its view model and `async decide(decision)` to await a modal choice. UI updates from rapidly arriving progress events should be coalesced/throttled (for example, maximum five visible redraws per second) without losing stage changes or terminal results.
- Use the existing `Event`/`Decision`/`Result` dataclasses as the interface contract. An optional small event extension is acceptable if a meaningful screen is otherwise impossible; do not expose raw yt-dlp text directly to widgets.
- Leave backend setup, runtime checks, format selection, FFmpeg conversion, file verification, stage cleanup, and output publishing in the established core modules.
- Keep the queue worker owned by the app so a modal closing does not cancel it implicitly. Resolve Stop or Quit asynchronously, keep the screen responsive while cleanup runs, and release the queue lock only after owned workers have settled. On abrupt parent loss, the supervisor terminates the subprocess tree. On reopening, use the durable row and core plan to reconcile its outcome.

### Backend startup and reuse

- Load operational preferences inside the TUI. If the first read fails, open the TUI, restore and reconcile the queue locally, and show **Configuration error** with the file path and reason. Use built-in preference values to initialize the form and Settings for repair. Keep draft editing, Settings, Details, Recent jobs browsing, Recheck, reordering and removal, Clear completed, Recover queue, and Quit available under their normal gates. Disable Add, Start/Resume, and all media retries, including Recent jobs retry, until **Save Settings** succeeds or **Retry backend** reads valid preferences. No startup backend check runs in this repair state. A successful Retry backend read applies the repaired defaults to the form, preserving its URL draft and scope, then checks without arming downloads. Repairing the file outside the TUI leaves the actions disabled until one of those controls succeeds in loading or saving valid preferences.
- With valid initial preferences, start one asynchronous `backend.ensure_backend()` check after the TUI admits the durable queue or starts with an empty index. If startup storage recovery is needed, defer this check until recovery succeeds. Follow core install, update, `auto_update`, and usable-version fallback rules. This worker never starts YouTube media work or installs Deno. The queue and form remain usable while the check runs, subject to storage and verification gates.
- Start/Resume or Retry can wait on an in-flight check after queue verification allows the action. The request is armed only in memory and rows retain their state until a usable backend is ready. A check for the current preferences that yields a usable fallback counts as ready and shows a warning. A hard failure for the current preferences disarms execution and leaves rows intact. Offer **Retry backend** to check again without arming downloads. A new Start/Retry request may also try the check again. Success can start media work only if an explicit request remains armed and the queue gates are open.
- If Stop or Ctrl+C arrives while a request is waiting, disarm it before cancelling and awaiting the updater. Stop leaves untouched rows unchanged. Confirmed Quit also cancels and awaits the updater and any active operation before exit. An updater completion that races with Stop or Quit cannot start a new row.
- Re-read preferences when arming a queue action, when a backend check settles, and before saving Active after backend preparation. Reuse readiness while the checked channel and auto-update values match the current preferences. If they change while a request is waiting, let the old check settle, ignore its result, and run one check with the latest preferences if the request is still armed. A change while idle leaves **Check pending** until requested. An active attempt keeps its current settings.
- If a preference read fails after startup, show **Configuration error** with the file path and reason. Invalidate readiness, disarm scheduling and let any active attempt settle. Keep the draft and existing rows, and populate Settings from the last valid read. Disable Add until repaired. Retry backend, Start/Resume or a media retry may re-read the file and run a fresh check. Saving valid Settings in either repair state leaves the queue idle with **Check pending**. The next backend or media action runs the check.
- The TUI uses the shared `backend.ensure_backend()` check and one updater task. Check that the managed executable is usable before dispatch. If it is missing or unusable, run the normal backend check once before saving Active. Disarm the action if that check fails without a usable fallback. After an admitted check, the runner uses the installed executable at the managed path and skips the redundant release check. FFmpeg and JavaScript runtime checks remain in the runner. The TUI starts its own updates between attempts. If an outside update interrupts a track, surface the core failure and let the user retry the saved plan with the installed executable.
- Publish a structured backend status to the frontend with the installed version when known. Show **Check pending** before preparation or after a preference change, **Checking** during a release check, and **Updating** during installation or replacement. Show **Up to date** after a successful comparison or installation, **Ready · updates off** when a usable backend skipped its update check, **Ready · using installed version** with a warning after fallback, and **Unavailable** after hard failure. Derive these states in the shared backend layer and preserve the existing CLI messages.

### State model

The TUI may track transient view state (URL field, selected options, active job ID, display snapshots, selected queue row, modal focus). The existing config and job store remain the sole authorities for durable preferences and **actual download status**. The only new persistent state is the small versioned queue index described above; do not create a separate SQLite DB, competing per-track job records, or background scheduler. It must be durably saved before the first transfer and reconciled with the existing job store on every launch.

Use output escaping for untrusted YouTube titles, metadata, errors, and paths, including Rich/Textual markup. Do not interpret metadata as layout instructions. Do not log unnecessary secrets or automatically import browser cookies.

## 7. Error and interruption behavior

- An invalid URL stays on the screen with an inline error.
- Missing Textual displays installation instructions without affecting other CLI commands.
- Missing FFmpeg or JS runtime shows the existing actionable diagnostic; if the runner requests Deno installation consent, use a modal and keep the TUI responsive.
- yt-dlp update failure preserves existing fallback behavior and shows a nonfatal warning when an installed backend still works.
- Partial download, network failure, unavailable media, collision, or conversion failure surfaces the summarized reason and the log path when present.
- Once the core chooses a retained video's exact target name and there is no saved video receipt, durably record the target basename in the item plan before checking whether that target is occupied. Keep this name separate from verification receipts. If saving it fails, report an item save failure before publishing either output for that item. Inspection uses the receipt name when available and otherwise the saved target name. An occupied saved target without a matching receipt is a conflict. An absent target is unfinished. The saved name lets Recheck and restore detect a conflict even on first publication. When a later retry has no video receipt, use the existing container selection and update the saved target if needed. A verified receipt retains the core's existing retry and remux rules.
- When the core detects an output conflict, preserve the occupied file and continue remaining playlist items. Save Failed with the conflict diagnostic when the attempt settles and show **Needs attention**. If a destination change also stops that attempt, keep Failed and report both problems. Continue to the next eligible URL if scheduling is still armed. Needs attention rows are ineligible for Start/Resume and retries. Recheck or restore shows Failed once the conflict is gone or Complete if all outputs verify. An external job shows Needs attention in Recent jobs. Ordinary CLI callers keep the existing collision behavior and exit codes.
- On Stop, preserve pending URLs, saved plans, completed files and partials. If the interrupted state cannot be saved, use the queue storage recovery flow. A job that is still owned by another process shows Needs attention with `Job already running` and can be rechecked after release.
- Resize keeps the current job and queue intact. Closing the terminal or losing an SSH session uses the same process ownership and restore rules as abrupt exit.

## 8. Compatibility / non-goals

**Must preserve:** Existing `ydl URL`, flags, prompts, config, default output path, MP3 quality semantics, yt-dlp update channel, video retention, cancellation, and `ydl retry` behavior. Scripts must keep working and the base package must remain installable without Textual.

**Outside this release.** Playback, thumbnails, album art, duration badges, desktop clipboard reads, theme selection, in-place queue option editing, per-track retry, and true process pause. Use Stop followed by Resume for interruption and recovery. Media library management, YouTube search, parallel downloads, background downloading after terminal exit, scheduling, browser integration, cookies/login UI, a web server, a native desktop window and full audio tagging stay outside scope.

## 9. Tests and release acceptance

Use Textual Pilot and a fake runner to test the UI. Exercise crash ownership and lock lifetimes at the real shared subprocess and job-store boundary with local test processes. Keep both test paths independent of YouTube. Run CI in separate base and TUI installations. The base runs the CLI tests and checks the missing-Textual message with Textual absent. The TUI job installs the `tui` extra and runs the TUI suite on the minimum and newest Python versions in the supported CI matrix. If a `.tcss` file is used, verify that it is included in the installed wheel.

1. **Launch.** `ydl tui` opens and focuses URL entry on a supported TTY. `ydl tui URL` pre-fills the field and leaves the queue unchanged and idle. Missing Textual and unsupported terminals return the documented plain-text remedies. `ydl tui --help` works without Textual or a TTY.
2. **Compatibility.** Existing CLI behavior, tests, and base package installation continue to work with Textual absent.
3. **Single item:** pasting a watch URL, retaining default VBR 0 and video-off, adding to queue, and starting it calls the existing runner with exactly those values.
4. **Playlist safety.** Auto defaults to This video for `watch?v=...&list=...`; choosing playlist uses the existing playlist path and any size confirmation. Dismissing a modal affects only its entry. Stop while the same modal is open prevents any later entry from starting.
5. **Options and Settings.** The typed destination and Browse confirmation affect the correct field. Browse Cancel preserves its text. Saving General and Backend options writes one config update, including Auto-update, and updates defaults for future entries without changing queue snapshots. Saving a channel or policy change during a job applies before the next operation. Cancel leaves the config intact.
6. **Responsive progress:** the startup backend check and fake slow metadata, update, transfer and conversion operations do not freeze keyboard navigation or redraws. The serial queue waits for its startup check and uses the checked backend; show indeterminate progress where no metrics are present.
7. **Cancel and quit.** Ctrl+C and confirmed Quit stop work during media processing or while waiting for backend preparation. Pending entries remain intact and the next URL never starts. Declining Quit leaves the prior operation in place. Stop still cancels and settles work if saving the interruption fails.
8. **Retry and locking.** Pause TUI verification after loading the receipts. A concurrent CLI retry of that job reports it is busy. After verification releases the lock, CLI retry can finish the job and Recheck sees Complete. While any queue row is Verifying, Retry failed stays disabled. Once verification finishes, the action attempts eligible Failed rows in order. A confirmed pre-plan failure retries with the original ID and options.
9. **Errors:** missing dependencies, video unavailability, format errors, and invalid URLs result in readable on-screen diagnostics and accessible logs.
10. **Terminal sizes.** Operate by mouse and keyboard at 80×24 and after resizing. At 100 columns or more show the extra queue columns. On narrower screens retrieve their values from Details. Modals, Settings and Help return focus to the originating control.
11. **Serial batch.** Add A/B/C and run them in order, with all of B's playlist entries settled before C starts. Pending edits affect the next selection, invalid lines remain editable, and a duplicate within the same paste requires confirmation.
12. **Queue persistence.** Enqueue A/B/C, reopen from another working directory, and recover their original order and absolute destinations while idle. After A completes, stop B before planning finishes and reopen. Show A Complete, B Pending and C Pending. Start plans B with its original ID and options. Repeat with a stop after the plan is durable. B returns as Interrupted and Resume retries that plan before C.
13. **Save recovery.** Fail Add before replacement and preserve the previous queue and draft. Fail Add after replacement but before directory sync and verify scheduling remains disabled until Recover queue can sync the file and reconcile it. Successful recovery stays idle and retains only the uncommitted draft lines. A corrupt queue remains intact and unscheduled, with a message that identifies the file and the recovery choices.
14. **Hard kill.** Queue A and B. SIGKILL the frontend while a descendant writes A's staging files and hold that tree in cleanup. Launch a second TUI and run `ydl retry` for A during the delay. Both exit `1`, and B cannot start. Verify the old writers exit, the TUI opens idle, and Resume can process the saved A plan followed by B. Keep the existing cooperative Ctrl+C test.
15. **Integrity and missing plans.** Verify Complete is saved when all outputs match, even if the previous session closed before saving the result. A modified output requires attention. Remove a Failed row's plan after dispatch but before the runner loads it. Verify Needs attention, a saved Failed hint, and no attempt to replan. Restore the plan and Recheck to enable Retry failed. On restart, an Active row with no plan shows Needs attention and describes the explicit Add option. Clearing completed rows preserves media, plans and logs.
16. **Changed availability.** Reopen with a pending URL that has disappeared and show its Failed entry while the next URL succeeds. Resume a saved playlist whose original URL and one unfinished video are gone. Use saved IDs, keep verified files, finish the available entries, and advance the URL queue after marking the playlist Failed. Reopening a fully verified row makes no YouTube request.
17. **Backend reuse and races.** New downloads and saved retries use one normal release check under unchanged settings. Verify usable fallback and hard failure with untouched entries. Arm Start during Nightly preparation, save Stable, and let the obsolete Nightly check finish with a hard failure. Verify the same request waits for exactly one Stable check and starts only after that check succeeds. An obsolete success also cannot launch a row. Stop during the wait prevents both a later callback and a successful current check from starting work.
18. **Packaging.** Install `.[tui]` through pipx and launch the installed command on Linux. Verify the test suite exercised the TUI extra and the tested Textual range in package metadata.
19. **Actions and shortcuts.** Check the primary Start/Resume/Stop states, selected retry versus batch retry, pending reorder, and index-only removal and clear confirmations. Exercise row menu and shortcuts by keyboard. Typing into the URL editor never fires the single-letter shortcuts.
20. **Metadata and Details.** The initial scope dialog and unplanned rows render without a YouTube metadata request. Track percentages, position and unknown values render as specified. Details stays tied to the selected job, shows available Info fields, and reads a bounded log with a usable missing-file state.
21. **Mouse workflow.** Use mouse controls to add a draft, select a row, open Details, scroll a long list, choose an output directory, save Settings, reorder pending rows, confirm removal, start, retry, stop and quit. Verify that the action menu is clickable at 80×24 and that the mouse can stop or quit while a runner modal covers the main screen. Disabled actions enforce the same rules as keyboard shortcuts. Verify the keyboard workflow remains available without mouse reports.
22. **Result during storage recovery.** Start an Add save before a playlist confirmation opens, and make it fail after replacement while the prompt is open. Choose Yes and verify the invoked runner settles using its plan, the next row stays idle, and Recover queue saves the buffered outcome. Choosing No instead buffers Cancelled with the decline diagnostic.
23. **Destination changes.** Put a symlink at a queued destination before dispatch and verify Needs attention. Replace it with a real directory and Recheck. In a second fixture, replace the folder between completed conversion and publication. Verify the check stops publication, preserves the job plan and earlier outputs, and identifies the path. Put the original folder back and Recheck before resuming.
24. **Backend changes.** Remove the managed executable after startup and verify dispatch runs the normal backend check before saving Active. If the check fails, the row remains unchanged. Simulate an outside change that causes a track to fail during a playlist. Retry uses the installed backend and the original track list, keeping verified files.
25. **Uncertain plan save.** Fail the jobs-directory sync after initial plan replacement. Verify no media starts and the row shows Needs attention. Restore storage, Recheck, and Resume the surviving plan at the same ID. If the plan is missing, show the explicit fresh-start remedy.
26. **Dispatch during queue edits.** Pause backend preparation before Active, remove the first Pending row, and move another Pending row to the front. The remaining entries run in the revised order. Stop after Active is durable and before runner invocation and verify the previous hint is restored.
27. **Live output conflict.** Occupy a selected retained-video target before first publication. Verify the file survives, a later track and the next queued URL run, and the row shows Needs attention. Move the file aside and Recheck to enable Retry failed.
28. **Unreadable preferences.** Make the config invalid while a backend check is running or before the next queue row. Verify that no new row starts and the message names the config file. Repair and explicitly request fresh backend readiness.
29. **Invalid configuration at launch.** Verify local queue restore, built-in Settings values, a configuration error with the path, and disabled Add and media actions. Save valid Settings to leave the queue idle with Check pending. Alternatively, repair the file externally and choose Retry backend to load it and check the backend while the queue stays idle. Both paths preserve the URL draft and scope.

A separate manual smoke test should use the previously troublesome `https://www.youtube.com/watch?v=vvpZOaQz6SM` link to verify that the TUI downloads and produces a playable MP3 through the *same* engine as the CLI. Passing the TUI test alone does not prove YouTube extraction still works.

## 10. Delivery order

1. **Reliable foundation.** Durable queue and job handoff, shared process ownership, restore and hard-kill checks, optional entry point, URL/options form, decisions, and one serial runner worker. Persistence and single-writer recovery are required in the first usable build.
2. **Operational experience.** Structured progress, queue management, cancellation, result and error details, recent-job summaries and retry selection.
3. **Release validation.** Mouse and keyboard workflows, terminal sizes, deterministic UI and package tests, documentation, and the live smoke test.

**Definition of done:** Someone unfamiliar with yt-dlp can paste one URL or a list of URLs into `ydl tui`, process them serially into MP3s, make informed playlist choices, reorder the queue, **close and reopen the TUI without losing pending work**, explicitly resume interrupted work, see progress, stop safely, and retry failed items without editing configuration or entering flags—while the original lightweight CLI remains fully compatible.
