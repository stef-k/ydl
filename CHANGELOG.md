# Changelog

## Unreleased

- Use playlist titles as folder names for new downloads. If a name is occupied,
  use the next available numbered name, starting with `(2)`. Retries use the
  original folder recorded in the saved job.

## 1.2.0 - 2026-11-27

- Add **Remove from queue on success** for TUI batches, with an unchecked default
  in Settings → General. Remove each opted-in row after a successful download or
  retry verifies every requested output. Keep the saved job and media available
  in Recent jobs.
- Keep the removal checkbox label readable when resizing the dashboard, including
  at the width where the queue adds its Type, Track progress and Output columns.

## 1.1.0 - 2026-11-25

- Use video titles without appended IDs for new media filenames. Saved jobs
  continue to use their original filenames.
- Open the download folder, MP3 or retained video from Job Details for
  completed jobs in the queue and Recent jobs. Open rechecks media even when
  opening a folder. It rechecks folder access at launch and detects edits to
  saved files, including through writable memory maps. Unrelated files in the
  folder do not interrupt Open.
  If Linux cannot start monitoring the files, Open still checks their receipts.
  Recent jobs shows the latest verified status and offers **Retry selected job**
  for eligible missing downloads.
- Limit Quit and Ctrl+Q to the main TUI screen. Quit cancels in-progress Open
  verification and pending desktop launches, and waits for verification to
  release the job lock.

## 1.0.0 - 2026-11-24

- Add the optional `ydl tui` frontend with a persistent serial queue, playlist
  choices, responsive settings and job details, and saved-job retry.
- Show a saved playlist's folder in the queue when its destination needs attention.
- Preserve recent-job retry results, reuse queue verification for linked jobs,
  and refresh job details after Recheck.
- Release superseded Recent jobs verification locks during queue recovery and
  show saved plan details after a destination change.
- Keep a saved plan required after a destination change and refresh Recent jobs
  when a queue entry is removed.
- Explain missing saved plans in Recent jobs and keep external retries out of
  the queue summary.
- Block retry when a queued job loses its saved plan during settlement, clear
  the warning after a successful Recheck, and keep queue counts readable at
  compact terminal widths.
- Keep the focused queue row visible during keyboard navigation and terminal
  resizing. When the live summary wraps, keep the selected row in view if
  visible and preserve the browsing position if the user has scrolled away.
  Match mouse presses and releases to the same row. Only consecutive clicks
  on the same row open Job Details.
- Keep job ownership until managed command trees finish cleanup after a stop
  or terminal loss.
- Add the `ydl` CLI for individual YouTube videos and playlists.
- Manage private yt-dlp and optional Deno installations, including release
  checks and SHA-256 verification.
- Show two-row interactive progress and support cancellation and retry from a
  saved download plan.
- Add user preferences, diagnostics, and automated CLI tests.
