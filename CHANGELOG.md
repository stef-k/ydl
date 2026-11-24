# Changelog

## Unreleased

- Add the optional `ydl tui` frontend with a persistent serial queue, playlist
  choices, responsive settings and job details, and saved-job retry.
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
  resizing, including when live progress wraps the queue summary. Keep the
  table in place when clicking a queue row so double-click opens that job.
- Keep job ownership until managed command trees finish cleanup after a stop
  or terminal loss.
- Add the `ydl` CLI for individual YouTube videos and playlists.
- Manage private yt-dlp and optional Deno installations, including release
  checks and SHA-256 verification.
- Show two-row interactive progress and support cancellation and retry from a
  saved download plan.
- Add user preferences, diagnostics, and automated CLI tests.
