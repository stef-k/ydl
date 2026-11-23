# Changelog

## Unreleased

- Add the optional `ydl tui` frontend with a persistent serial queue, playlist
  choices, responsive settings and job details, and saved-job retry.
- Preserve recent-job retry results, reuse queue verification for linked jobs,
  and refresh job details after Recheck.
- Release superseded Recent jobs verification locks during queue recovery and
  show saved plan details after a destination change.
- Keep job ownership until managed command trees finish cleanup after a stop
  or terminal loss.
- Add the `ydl` CLI for individual YouTube videos and playlists.
- Manage private yt-dlp and optional Deno installations, including release
  checks and SHA-256 verification.
- Show two-row interactive progress and support cancellation and retry from a
  saved download plan.
- Add user preferences, diagnostics, and automated CLI tests.
