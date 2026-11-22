# ydl — Terminal UI (TUI) Design Specification

**Status:** Proposed, iteration 3 — queue ownership and recovery\
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
- Require an interactive TTY for `ydl tui`. If unavailable or the terminal lacks basic capabilities, fail with a plain-text hint to use `ydl URL`. Avoid escape sequences before capability checks.

**Framework choice:** Use [Textual](https://textual.textualize.io/) (MIT) as an **optional** dependency. It already supplies responsive keyboard navigation, async workers, widgets, layout, and terminal-size handling. Do not create a homegrown curses framework or introduce Qt, GTK, Flatpak, Electron, or a persistent service.

Suggested packaging:

```toml
[project.optional-dependencies]
tui = ["textual"]  # Constrain to a tested compatible range during implementation.
```

From the checkout, install with `pipx install '.[tui]'`. An existing pipx installation can be replaced with `pipx install --force '.[tui]'` so both routes use the same tested dependency range. Parse `ydl tui --help` before checking terminal capabilities or importing Textual. For a missing optional dependency or unsupported terminal, exit `1` with a plain-text remedy. The base CLI continues to work without Textual. Developer-only Textual tooling stays outside the runtime extra.

## 4. Layout and interaction

One main screen is sufficient. It should fit comfortably at 80 columns by 24 rows and rearrange or collapse secondary panels in narrower terminals; never require mouse input. Prioritize the active transfer and queue over recent-history decoration.

```text
┌─ ydl ────────────────────────────── Backend: ready ─────────────┐
│ URLs [ Paste one or multiple links here ]  [ Add to queue ]     │
│ Scope: [ Auto ▼ ]  Quality: [ VBR 0 ▼ ]  Keep video: [ ]        │
│ Save to: [ ~/Downloads/ydl                                ]    │
│ [ Start queue ]  [ Stop queue ]  [ Retry failed ] [ Settings ]   │
├─ Queue — 1 active / 2 pending / 1 complete / 0 failed ──────────┤
│ ▶ 1. Playlist A    3/24  Downloading  56%  ETA 00:18            │
│   2. Video B       Pending                                    │
│   3. Video C       Pending                                    │
│ ✓ 4. Video D       Complete                                   │
├─ Active item ───────────────────────────────────────────────────┤
│ Track title   [████████░░░░░░░░░░] 56%   2.1 MiB/s             │
└─ Tab navigate · Enter select · Ctrl+C stop · Ctrl+Q quit ──────┘
```

The mockup is illustrative, not pixel-prescriptive. Prefer a calm, information-dense layout with readable statuses rather than animations, album artwork, or decorative panels.

### Serial URL queue (required)

- Use the same Add URLs dialog for one URL or newline-separated URLs. Trim blank lines. Validate each line with `media.parse_url` and validate its selected scope with `media.validate_scope`. Commit the accepted entries together and keep rejected lines editable with their errors. Check duplicates against pending rows and the accepted lines of the current batch.
- Each accepted URL becomes a separate queue entry. Save its canonical URL, requested scope, quality, video-retention choice, and destination. Expand `~` and resolve relative output paths at enqueue time so reopening from a different working directory preserves the destination. Map the stored `auto` scope to `None` at the runner boundary. The current configuration supplies defaults for new entries. Form edits leave saved entries unchanged.
- Show an ordered list with the durable states below and transient stages such as Checking, Downloading, and Converting. Expose **Add**, **Remove pending**, **Move up/down pending**, **Start/Resume queue**, and **Retry failed**. While running, edits apply to pending rows only. Idle users may remove any inactive row or clear all completed rows. These actions remove queue entries only and leave job records, logs, partials, and published files in place.
- Execute entries **strictly one at a time**: `await runner.download(...)` or `await runner.retry(...)` must finish (including conversion, verification, and publication) before the next starts. One queued playlist itself runs items serially using the existing runner. No parallel URL jobs, worker pool, or additional yt-dlp instances for media downloads.
- **Start/Resume queue** processes Pending and Interrupted rows in their current displayed order. Additions, removals, and moves affect the next selection. **Retry failed** makes one pass over the Failed rows present when pressed, in queue order. It leaves Pending and Interrupted rows for Start/Resume. Each action attempts a row at most once. Both actions advance after an entry settles and its result is saved.
- A failure stores the core diagnostic and exposes the job ID and log path when available. A refusal or dismissal follows the decision outcomes below. A recoverable entry failure leaves later entries eligible to run during the same action.
- **Stop queue** and Ctrl+C close the scheduling gate immediately, cancel the active operation, and await subprocess cleanup. Record an active row as Interrupted after it settles. Keep its job plan and partials, along with all other rows and finished files. Start/Resume is available again after cleanup and a successful queue save or reload.
- A brief queue summary reports count of queued URLs, completed URLs, failed URLs, and active playlist item position. Do not equate a playlist of 100 tracks with 100 queued URLs; distinguish URL queue progress from the existing per-playlist item progress.
- Ask before adding an exact canonical duplicate of a pending or accepted batch URL. Rejected duplicates stay out of the transaction. The core retains its filename collision rules for distinct entries that resolve to the same output.

### Persistent queue and restart recovery (required for the first TUI release)

**User-facing contract.** A saved queue survives application exits, terminal closure, a Linux reboot, and unexpected interruption. It keeps pending URLs, their order and options, and the association with started jobs. Opening `ydl tui` restores the queue and leaves it idle until the user presses **Start/Resume queue** or **Retry failed**.

**Storage and ownership**

- Use a schema-versioned JSON document named `tui-queue.json` under the `Paths.state` directory. `JobStore` owns media plans, verified outputs and retries. The queue document stores ordered rows with unique UUID4 `id` values in 32-character lowercase hexadecimal form. Each ID is also reserved as the future core job ID. Store the canonical URL, requested scope, quality, retention flag, resolved output directory, a lifecycle hint (`pending`, `active`, `complete`, `failed`, `cancelled`, `interrupted`), and any entry diagnostic. Progress, credentials and fragments stay outside the queue file.
- Canonicalize through the core URL parser. Keep the normalized YouTube host and relevant video and playlist IDs, including both IDs when present. Drop tracking parameters. Running a saved row must allow the same scope choices as its original URL.
- Take a nonblocking exclusive POSIX lock on a dedicated queue lock file before restoring the queue or starting backend work. Hold it for the session, including shutdown. A second TUI exits `1` with a plain-text message identifying the busy queue. CLI downloads continue to use independent per-job ownership.
- Sync each accepted queue mutation as one transaction before reporting success. Write a temporary file beside the queue file, `fsync` the file, replace the queue file atomically, and `fsync` the directory, including any newly created parent entries needed to reach it. Save Active before invoking the runner and save the settled status before advancing to another row. Progress redraws do not write the queue file.
- A failure before replacement rejects the change and keeps the prior saved state. A failure after replacement but before directory sync leaves durability uncertain. Show that outcome, disarm scheduling, and disable further queue mutations until a successful reload. A failed lifecycle save also stops advancement. Stop and Quit must still cancel and settle active work even when storage is unavailable. On reopening, use the last readable queue file and reconcile it against the job store.

**Job handoff and process ownership**

- Add a validated optional preassigned ID to `runner.download()`. Ordinary CLI downloads keep automatic IDs. For a new queue attempt, the core must acquire the per-job lock before checking for a plan at that ID. Under the lock, durably create a new plan only if none exists. Treat an existing plan, including an unreadable one, as already occupied. Return a distinguishable existing-plan or busy outcome so the TUI can reconcile it. The core holds the lock through execution and cleanup.
- Keep the queue ID durable before invocation. Persist the core plan with a file sync, atomic replacement and directory sync before any media transfer, including the initial creation of the jobs directory. Persist verification receipts durably before publishing their output filenames. The TUI reads these records through the core and never rewrites a plan. If the plan is absent on recovery, the same ID and enqueued options can be used to plan again. Before a plan is saved, scope and playlist consent may be requested again. Once a plan exists, all media attempts use `runner.retry(id)` with its original resolved options and item order.
- Use a short-lived Linux supervisor for each managed command tree. It observes loss of its parent through a lifetime pipe and terminates and reaps the tree on parent loss. For a job command, pass the supervisor a copy of the job lock before a writer starts. Retain that lock until every process that can write to the job has exited. Apply this to both new downloads and retry, including a CLI retry of a queue job.
- Resume uses the same core lock as the original attempt. If a previous supervisor is still cleaning up, show Needs attention with a reason that the job is busy. Neither the TUI nor `ydl retry` can write to that job until ownership is released. Normal Stop and Quit await cleanup before acknowledging completion.

**Restore behavior**

Restore and validate the queue locally. A missing queue file starts with an empty index. Show rows in saved order while the read-only integrity checks run asynchronously. Show Verifying while an integrity result is pending. A requested action waits for the checks needed to select its next row in order. Restore makes no YouTube requests. The separate backend startup check may contact the release service after the queue loads.

Reconcile using the following rules in order. Use the core's output verification and collision rules. A busy check may probe an existing lock without creating it or obtaining exclusive ownership. The core still takes the exclusive lock and rechecks the plan on every attempt.

| Saved plan and verified outputs | Restored row | Explicit action |
| --- | --- | --- |
| Job is busy, plan is unreadable, or an output conflicts with its verification record | Needs attention, with the reason | **Recheck** after the job releases ownership or the obstruction is addressed. |
| Every requested item has verified outputs, even if the TUI last saved Active or the core completion flag was still false | Complete | Clear completed to remove the row. No media work is needed. |
| Valid plan with unfinished items, and the last hint was Failed | Failed | **Retry failed** uses `runner.retry(id)`. |
| Valid plan with unfinished items, including missing files from a previously Complete row | Interrupted | **Start/Resume queue** uses `runner.retry(id)`. |
| No plan, and the last hint was Failed | Failed | **Retry failed** uses `runner.download()` with the original ID and options. |
| No plan, and the last hint was Cancelled | Cancelled | Remove the row or explicitly add the URL again. |
| No plan, and the last hint was Complete | Needs attention, with a missing-plan diagnostic | Recheck or remove the row. Automatic replanning is disabled for this case. |
| No plan, and the last hint was Pending, Active or Interrupted | Pending, with a planning note for interrupted work | **Start/Resume queue** uses `runner.download()` with the original ID and options. |

Verifying and Needs attention are derived view states. They leave the last durable hint intact. Recheck is available while idle and refreshes these states using local data. If the core reports an existing plan or busy job after the queue selects a row, reconcile it using its hint from before the attempt. Use `runner.retry(id)` only if the reconciled row is eligible for the current action. A busy result skips the row for the rest of the action and displays the reason. These inspection paths leave the plan and outputs intact.

On corrupt or unsupported queue JSON, leave the original file in place, show its path and an actionable recovery message, and disable mutations, backend startup and scheduling. Core jobs remain available through `ydl retry JOB_ID`.

**YouTube availability can change between sessions**

- A queued URL with no saved plan is resolved when it is explicitly attempted. If YouTube reports the video or playlist unavailable, removed or private, save the row as Failed with the core diagnostic. Keep its URL, options and ID. Advance to the next eligible row. A later Retry failed can try the same entry again.
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

- Focus the URL entry on startup. Normal terminal paste (`Ctrl+Shift+V`, mouse paste, etc.) works without application clipboard integration. A multiline paste is accepted by the Add URLs dialog; a single URL follows the same queue path.
- **Scope**: Auto (default), Single video, Entire playlist. Auto delegates to existing `runner.scope_for`; show a modal for video-plus-playlist URLs. Pure playlist URLs use playlist scope. Preserve current large/unknown-playlist confirmation.
- **Quality**: built-in default VBR `0`. Offer VBR `0`–`9` and common explicit bitrates (`192K`, `256K`, `320K`). Include a saved valid bitrate in the chooser even when it differs from these presets. Use `quality_value` to validate the selected value.
- **Keep video**: checkbox, default from saved preferences. Pass its selected value explicitly to the runner to avoid a duplicate yes/no prompt.
- **Destination**: editable path with default from preferences. Validate via the existing path code. No built-in filesystem browser required.
- Adding a URL snapshots the current values for that queue entry. Settings can explicitly save the default quality, video retention, output directory and release channel. Validate and write the edited defaults together through the shared config code. Scope remains a per-entry choice and `auto_update` keeps its existing configured value.
- If the URL or field is invalid, focus the affected control, show an inline actionable error, and leave the user-entered data intact.

### Decisions and confirmations

Render the core's `Decision` question, options and default in a modal. Enter activates the focused choice, initially the core default. Await the response in the app-owned serial worker. Always settle or cancel the pending wait before leaving the dialog.

- In Auto scope, a video with playlist context defaults to **This video**. Run TUI jobs with interactive decisions enabled and keep the core's confirmation for large or unknown-size playlists.
- Choosing **No** to a playlist confirmation uses the core's declined result and saves Cancelled. Choosing **No** to Deno installation uses the core's missing-runtime failure and saves Failed. Both allow the next queue entry to run after settlement.
- Escape or Cancel in a runner dialog aborts only the current attempt. Return a distinct dismissal outcome to the scheduler. Save Cancelled if the attempt has no plan, or Interrupted if an existing plan is retained. Continue with the next eligible row, skipping the dismissed row for the rest of this action. A dismissed pre-plan dialog creates no job plan or transfer.
- Stop queue and Ctrl+C close the scheduling gate even while a runner dialog is visible. Dismiss the dialog, cancel and await the operation, and preserve a settled terminal result if one was already committed. Confirmed Quit uses the same stop path before exit. Declining Quit leaves the prior dialog or operation in place.

### Job and results panel

Show per-URL queue counts and the backend/update state, current stage (`checking`, `updating`, `metadata`, `downloading`, `converting`, `saved`, `failed`), title, playlist position and total when known, progress percentage, speed, ETA, completed/failed count, and destination. Use indeterminate progress when no reliable total exists. Do not invent conversion percentages or whole-playlist ETA.

After completion show a short summary and output path. On failure show the human-readable core diagnostic. Offer to reveal or copy the log path when a log file exists, including for failures during planning. Before a plan exists, identify the queue row by its reserved ID and distinguish it from a job that `ydl retry` can load.

### Recent jobs and retry

Show a small bounded list of recent saved jobs. For jobs without queue history, display Verifying, Complete after output verification, Incomplete, or Needs attention with the reason. Use the queue's Failed and Interrupted distinctions for linked rows and Active for work currently owned by this TUI. The core records do not need a new history field. **Retry selected job** runs `runner.retry(id)` for that job through the same serial worker and backend gate. When it belongs to a queue row, update that row through the same durable transitions. External jobs stay outside the queue index.

Share a read-only inspection boundary for queue reconciliation and the recent-jobs list. Apply the same verification receipts used by retry. The query leaves job state untouched, uses no exclusive claim, and does not create a lock file. A malformed historical record is flagged without preventing fresh downloads.

## 5. Keyboard behavior

- `Tab` / `Shift+Tab`: move between interactive controls; arrow keys navigate selections.
- `Enter`: activate focused control or confirm the currently focused dialog choice.
- `Escape`: close the current dialog, or return focus without changing state.
- `Ctrl+C`: stop a running queue or cancel a Start/Retry request waiting on backend preparation. It has no queue effect when idle.
- `Ctrl+Q`: quit. If work is active or a queue action is armed, ask whether to stop and exit or stay. Cancel and await any startup worker before exit, including when there is no running job.
- Provide visible Help (`?` or an explicit Help control), including shortcuts. Avoid global single-letter shortcuts firing while a text input has focus.

An active job is not an invitation to run another one. While a queue is running, allow adding pending URLs and viewing status, but disable starting another runner/queue. One active runner operation at a time, across all URLs and playlist items, is the TUI concurrency contract.

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

- Once the TUI owns a valid queue, start one asynchronous `backend.ensure_backend()` check. Follow core install, update, `auto_update`, and usable-version fallback rules. Opening the TUI may bootstrap a missing yt-dlp or check the selected release channel. This worker never starts YouTube media work or installs Deno. The queue and form remain usable while the check runs.
- Start/Resume or Retry can wait on that in-flight check. The request is armed only in memory and rows retain their states until a usable backend is ready. A check failure with a usable fallback counts as ready and shows a warning. A hard failure disarms pending execution and leaves rows intact. Offer **Retry backend** to check again without arming downloads. A new Start/Retry request may also try the check again. A successful check starts media work only if an explicit request remains armed.
- If Stop or Ctrl+C arrives while a request is waiting, disarm it before cancelling and awaiting the updater. Stop leaves untouched rows unchanged. Confirmed Quit also cancels and awaits the updater and any active operation before exit. An updater completion that races with Stop or Quit cannot start a new row.
- A successful check, including a usable fallback, covers subsequent work under the same configured channel and `auto_update` policy for that session. Re-read operational preferences before starting a queue action and before each new runner invocation. Changes to the channel or update policy invalidate readiness before the next job. Queue entries retain their enqueued media options. If the core reports that the executable has become missing or unusable, show that error, retain the row's pre-attempt hint, invalidate readiness and disarm scheduling. The next explicit Start or Retry checks again.
- Route both `runner.download()` and `runner.retry()` through this gate and pass `no_update=True` to each. Add the optional `no_update` argument to `runner.retry()` with its current behavior as the default. Use the core installation lock and a single TUI updater task. Never run updates concurrently with TUI media operations. FFmpeg and runtime checks remain in the runner.

### State model

The TUI may track transient view state (URL field, selected options, active job ID, display snapshots, selected queue row, modal focus). The existing config and job store remain the sole authorities for durable preferences and **actual download status**. The only new persistent state is the small versioned queue index described above; do not create a separate SQLite DB, competing per-track job records, or background scheduler. It must be durably saved before the first transfer and reconciled with the existing job store on every launch.

Use output escaping for untrusted YouTube titles, metadata, errors, and paths, including Rich/Textual markup. Do not interpret metadata as layout instructions. Do not log unnecessary secrets or automatically import browser cookies.

## 7. Error and interruption behavior

- An invalid URL stays on the screen with an inline error.
- Missing Textual displays installation instructions without affecting other CLI commands.
- Missing FFmpeg or JS runtime shows the existing actionable diagnostic; if the runner requests Deno installation consent, use a modal and keep the TUI responsive.
- yt-dlp update failure preserves existing fallback behavior and shows a nonfatal warning when an installed backend still works.
- Partial download, network failure, unavailable media, collision, or conversion failure surfaces the summarized reason and the log path when present.
- On Stop, preserve pending URLs, saved plans, completed files and partials. Save the interrupted state when possible and reconcile it on reopening if the save failed. A job that is still owned by another process shows Needs attention with `Job already running` and can be rechecked after release.
- Resize keeps the current job and queue intact. Closing the terminal or losing an SSH session uses the same process ownership and restore rules as abrupt exit.

## 8. Compatibility / non-goals

**Must preserve:** Existing `ydl URL`, flags, prompts, config, default output path, MP3 quality semantics, yt-dlp update channel, video retention, cancellation, and `ydl retry` behavior. Scripts must keep working and the base package must remain installable without Textual.

**Not in this iteration:** Playback, thumbnails or album art, media library management, search within YouTube, **parallel downloads**, background downloading after terminal exit, pause button (stop + retry is sufficient), scheduling, browser integration, cookies/login UI, remote web server, native desktop window, and full audio tagging.

## 9. Tests and release acceptance

Use Textual Pilot and a fake runner to test the UI. Exercise crash ownership and lock lifetimes at the real shared subprocess and job-store boundary with local test processes. Keep both test paths independent of YouTube. Run CI in separate base and TUI installations. The base runs the CLI tests and checks the missing-Textual message with Textual absent. The TUI job installs the `tui` extra and runs the TUI suite on the minimum and newest Python versions in the supported CI matrix. If a `.tcss` file is used, verify that it is included in the installed wheel.

1. **Launch.** `ydl tui` opens and focuses URL entry on a supported TTY. `ydl tui URL` pre-fills the field and leaves the queue unchanged and idle. Missing Textual and unsupported terminals return the documented plain-text remedies. `ydl tui --help` works without Textual or a TTY.
2. **Compatibility.** Existing CLI behavior, tests, and base package installation continue to work with Textual absent.
3. **Single item:** pasting a watch URL, retaining default VBR 0 and video-off, adding to queue, and starting it calls the existing runner with exactly those values.
4. **Playlist safety.** Auto defaults to This video for `watch?v=...&list=...`; choosing playlist uses the existing playlist path and any size confirmation. Dismissing a modal affects only its entry. Stop while the same modal is open prevents any later entry from starting.
5. **Options:** toggling keep-video, changing quality/output, and explicitly saving new defaults behave correctly; incidental job settings never overwrite config.
6. **Responsive progress:** the startup backend check and fake slow metadata, update, transfer and conversion operations do not freeze keyboard navigation or redraws. The serial queue waits for its startup check and uses the checked backend; show indeterminate progress where no metrics are present.
7. **Cancel and quit.** Ctrl+C and confirmed Quit stop work during media processing or while waiting for backend preparation. Pending entries remain intact and the next URL never starts. Declining Quit leaves the prior operation in place. Stop still cancels and settles work if saving the interruption fails.
8. **Retry and locking.** A locked job remains untouched. Retry failed attempts the Failed rows once and Start/Resume skips them. Retry uses the saved plan when present and the original ID and enqueue options when planning never completed. Recent jobs without queue history show only states the core can establish.
9. **Errors:** missing dependencies, video unavailability, format errors, and invalid URLs result in readable on-screen diagnostics and accessible logs.
10. **Terminal sizes:** operate by keyboard at 80×24, recover from terminal resize, and collapse secondary panels without hiding essential input/status controls.
11. **Serial batch.** Add A/B/C and run them in order, with all of B's playlist entries settled before C starts. Pending edits affect the next selection, invalid lines remain editable, and a duplicate within the same paste requires confirmation.
12. **Queue persistence.** Enqueue A/B/C, reopen from another working directory, and recover their original order and absolute destinations without starting work. Stop during B, reopen again, and show A Complete, B Interrupted, and C Pending. Resume uses B's original job ID before processing C.
13. **Crash consistency.** Interrupt just before the core plan is saved, just after it is durably saved, and just after work finishes but before the queue result is saved. Restore the expected states and reuse the same job ID. Verify contention during preassigned plan creation, and simulate queue save errors on both sides of replacement. A corrupt or unsupported queue stays intact and unscheduled.
14. **Hard kill.** SIGKILL the frontend during a command with a descendant writing staging files. Use a controlled shutdown delay to attempt both TUI resume and CLI retry while the old tree is settling. Both attempts must be refused. Verify the old writers exit and the original job can then resume. Keep the existing cooperative Ctrl+C test.
15. **Integrity.** A fully verified job is Complete even if the last saved hint was Active. Missing files from a completed job make it Interrupted and a modified output requires attention. Clearing completed rows leaves media, plans and logs intact.
16. **Changed availability.** Reopen with a pending URL that has disappeared and show its Failed entry while the next URL succeeds. Resume a saved playlist whose original URL and one unfinished video are gone. Use saved IDs, keep verified files, finish the available entries, and advance the URL queue after marking the playlist Failed. Reopening a fully verified row makes no YouTube request.
17. **Backend reuse.** A queue with new downloads and saved retries uses one normal release check under unchanged settings. Verify usable fallback, hard failure with untouched entries, and rechecking after a channel change. A stopped Start request cannot launch a row when a check later settles.
18. **Packaging.** Install `.[tui]` through pipx and launch the installed command on Linux. Verify the test suite exercised the TUI extra and the tested Textual range in package metadata.

A separate manual smoke test should use the previously troublesome `https://www.youtube.com/watch?v=vvpZOaQz6SM` link to verify that the TUI downloads and produces a playable MP3 through the *same* engine as the CLI. Passing the TUI test alone does not prove YouTube extraction still works.

## 10. Delivery order

1. **Reliable foundation.** Durable queue and job handoff, shared process ownership, restore and hard-kill checks, optional entry point, URL/options form, decisions, and one serial runner worker. Persistence and single-writer recovery are required in the first usable build.
2. **Operational experience.** Structured progress, queue management, cancellation, result and error details, recent-job summaries and retry selection.
3. **Release validation.** Keyboard-only behavior, terminal sizes, deterministic UI and package tests, documentation, and the live smoke test.

**Definition of done:** Someone unfamiliar with yt-dlp can paste one URL or a list of URLs into `ydl tui`, process them serially into MP3s, make informed playlist choices, reorder the queue, **close and reopen the TUI without losing pending work**, explicitly resume interrupted work, see progress, stop safely, and retry failed items without editing configuration or entering flags—while the original lightweight CLI remains fully compatible.
