# ydl — Python YouTube-to-MP3 CLI

**Specification:** v1.0\
**Target:** Linux Mint / Ubuntu, Python 3.11+\
**License:** MIT for the `ydl` project; third-party components retain their own licenses.

## 1. Goal

A single command should download a YouTube video or playlist as MP3 without requiring the user to understand yt-dlp, FFmpeg or JavaScript challenge solvers:

```bash
ydl https://www.youtube.com/watch?v=VIDEO_ID
```

The CLI must remain responsive, display meaningful progress, handle Ctrl+C, and resume interrupted work. It must never require root privileges to install or update its downloader backend.

**Meaning of non-blocking:** network checks, updates, metadata extraction, downloads and conversion execute in managed asynchronous subprocesses. `ydl URL` remains attached to the terminal for progress and cancellation; it is not a detached daemon.

## 2. Default behavior

1. Validate the URL and determine whether it identifies a single video, a playlist, or a video that also belongs to a playlist.
2. Bootstrap the private yt-dlp backend on first use. By default, check an existing backend for updates when a download or retry needs media work, using the nightly channel. Apply a verified update without root access and use it for the same run. Keep the previous usable version if the update fails.
3. If the URL contains both video and playlist context, ask **This video [default]** or **Entire playlist**. A pure playlist URL selects playlist scope. Confirm playlists with more than 50 entries or an unknown size before starting media downloads.
4. Ask whether to keep the original video unless a retention flag is supplied or prompts are disabled. Enter accepts the configured preference. The built-in preference is **No**, shown as `[y/N]`.
5. Download the best available audio-only format and convert it to MP3 using FFmpeg/LAME VBR quality `0` (best encoder quality). If only combined audio and video formats are available, download the best combined source temporarily for MP3 extraction.
6. Save completed files under `~/Downloads/ydl/`; delete temporary source media **only after** a verified MP3 has been created.
7. Show the resolved settings on one fixed row and live progress on a second row. Support cancellation, automatic transient-error retries and a subsequent retry of unfinished items.

**Quality limitation:** YouTube supplies lossy source audio. A high-bitrate MP3 cannot recreate detail missing from that source. “Highest MP3 quality” means the best available source followed by the highest MP3 encoder quality, **not** a claim of lossless audio.

## 3. CLI contract

```text
ydl URL [--single | --playlist] [--keep-video | --no-keep-video]
        [--quality QUALITY] [--output DIRECTORY] [--yes] [--no-update]

ydl retry [JOB_ID]
ydl doctor
ydl config show
ydl config set output DIRECTORY
ydl config set quality QUALITY
ydl config set keep_video true|false
ydl config set channel stable|nightly
ydl update [--check]
```

- `--single` selects the addressed video, even if `list=` is present. A URL with no video ID is an argument error.
- `--playlist` selects the referenced playlist. A URL with no playlist context is an argument error. Size confirmation follows the rules in section 5.
- `--keep-video`: additionally obtain and retain the best available playable video, with its audio, alongside the MP3.
- `--no-keep-video`: explicitly override a saved retention preference.
- `--quality`: FFmpeg audio quality `0`–`9` (VBR; `0` best) or an explicit bitrate such as `192K` or `320K`; default `0`.
- `--output`: expand `~`, create the directory if needed, and persist changes only through `config set`.
- `--yes` skips the retention prompt and uses the URL's default scope when no scope flag is supplied. It confirms a large or unknown-size playlist only when the resolved scope is playlist. Installing a JavaScript runtime still requires separate consent.
- `--no-update` uses the installed backend for this run and skips backend release lookup, installation and updates. A missing or unusable backend produces an error with exit code `1` and points to `ydl update`.
- `retry` uses the original job plan and resolved download settings. With no ID, select the most recently created incomplete job, breaking timestamp ties by highest job ID in lexical order. An unknown ID or absence of an eligible job exits `2`.
- `doctor`: check the backend, FFmpeg/ffprobe, JavaScript runtime, versions, filesystem permissions and configuration; report exact remedies.
- `update` installs or updates the backend to the latest release on the configured channel. `update --check` compares the installed backend with that release and reports the result without changing the installation. Section 4 defines both commands.

Reject conflicting flags with a clear message. Non-interactive stdin must not cause the program to hang: use supplied options or safe defaults, and fail clearly if explicit confirmation is necessary.

For configurable download options, explicit flags take precedence over saved preferences, followed by built-in defaults. `--yes` and non-interactive stdin use the resolved video-retention preference without a prompt.

### Example interaction

The scope and retention prompts are regular terminal lines. After the choices are settled, the settings and progress look like this.

```text
$ ydl 'https://www.youtube.com/watch?v=VIDEO_ID&list=PLAYLIST_ID'
Download: 1) This video [default]  2) Entire playlist: 2
Keep original videos? [y/N]: n
Playlist | MP3 VBR 0 | Keep video off | ~/Downloads/ydl/Playlist name/
[3/24] Downloading Track title | 68% | 2.1 MiB/s | ETA 00:12
```

The settings row stays in place. The next update can replace the progress row with `[3/24] Converting Track title…`. When the playlist finishes, the summary takes the progress row and ends with a newline.

```text
Completed: 24 MP3 files, 0 failed
```

## 4. Backend and dependencies

**Python:** Prefer the standard library (`argparse`, `asyncio`, `json`, `pathlib`, `shutil`, `signal`, `subprocess`, `tomllib`, `urllib.parse`) over extra packages. No GUI framework, background server or database.

**Downloader:** Use the official **yt-dlp Unix zipimport executable**, stored privately under `~/.local/share/ydl/bin/yt-dlp` (respect `XDG_DATA_HOME`). Do not modify or depend on the distro's `/usr/bin/yt-dlp`. The official zipimport release bundles `yt-dlp-ejs`, avoiding an independently stale challenge-solver package.

**Update channel.** Default to the official yt-dlp **nightly** channel. Use [yt-dlp/yt-dlp-nightly-builds](https://github.com/yt-dlp/yt-dlp-nightly-builds/releases) for `nightly` and [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp/releases) for `stable`.

**Update flow**

1. For a download or retry that needs media work, bootstrap a missing or unusable backend unless `--no-update` applies. For an already usable backend, contact the release service only when `auto_update` is enabled and `--no-update` does not apply. Retry uses the current backend configuration. A retry with all expected files already verified can finish using the saved job record alone. `config` and `doctor` run without installing or updating the backend. `ydl update` runs regardless of `auto_update`.
2. Look up the latest release in the configured channel's official repository over HTTPS. Read the SHA-256 for the `yt-dlp` Unix executable from that release's `SHA2-256SUMS`. The installed backend is up to date when it runs successfully and its digest matches. A saved channel change takes effect at the next automatic or explicit update, even if its release is older. Saving the setting itself leaves the installation untouched.
3. `ydl update --check` reports the installed version or that the backend is missing or unusable, the configured channel and latest release tag, and whether installation or replacement is available. A successful check exits `0` in all these cases. A lookup or checksum-manifest error exits `1`. It reads release metadata and the checksum manifest, leaving the binary and rollback copy untouched.
4. For installation or replacement, download the `yt-dlp` asset from the selected release to a staging file on the same filesystem. Verify its SHA-256 and require `--version` to match the release tag. Keep a copy of the previous usable executable and atomically publish the candidate only after verification. Keep the previous copy until the next successful replacement. Spawn new metadata and download subprocesses against the verified installation path.
5. If a bootstrap or automatic update fails, warn and proceed when the old backend still works. Otherwise exit `1`. An explicit `ydl update` reports failure and exits `1` even if the previous backend is still usable. Backend release operations have a 60-second deadline per command, support Ctrl+C and retain diagnostic details. Cancellation or failure before replacement leaves the usable backend in place.

**Conversion:** Require `ffmpeg` and `ffprobe` on `PATH`. Before a download or retry needs media processing, show one actionable error if either is absent. For default audio-only mode, prefer `bestaudio/best` followed by MP3 extraction (`-x --audio-format mp3 --audio-quality 0` at the default quality). For `--keep-video`, select the best playable video+audio combination, merge if required, extract MP3, and preserve the completed video. Keep the staging source through verification even in audio-only mode so `ydl` controls cleanup. Never assume YouTube offers a native MP3 stream or hard-code a format ID.

**YouTube JavaScript support:** Detect a compatible **Deno >= 2.3** or **Node >= 22** and pass its executable path explicitly to yt-dlp (`--js-runtimes`). Prefer an already installed compatible runtime, including one installed through `nvm` when visible on `PATH`. If none is found, offer a one-time, checksum-verified, unprivileged Deno installation into `ydl`'s user-data directory; require consent. Do not silently report a missing runtime as a requested-format failure.

**Process isolation:** Invoke yt-dlp as an external process using argument arrays, never `shell=True`. Pass `--ignore-config` for deterministic behavior, and use yt-dlp's machine-oriented `--progress-template` and `--print` output rather than parsing its human-readable progress bars.

## 5. URL and playlist behavior

- Accept ordinary `youtube.com/watch?v=…`, `youtu.be/…`, `youtube.com/shorts/…`, and `youtube.com/playlist?list=…` URLs, including `music.youtube.com` equivalents where yt-dlp supports them.
- Parse URL structure using `urllib.parse`, not substring matching. Normalize unnecessary tracking parameters without losing the video and playlist identifiers.
- Resolve scope from `--single` or `--playlist` first. Otherwise a video+playlist URL prompts for scope with **This video** as the default. `--yes` and non-interactive stdin select that default without prompting. A playlist-only URL selects the playlist.
- Probe playlist metadata without media downloads and read at most 51 entries. Count duplicate and unavailable entries in their original positions. Classify the playlist as large if the reported total exceeds 50 or the probe finds 51 entries. A reported total of 50 or fewer, or a probe that reaches the end within 50 entries, establishes a small playlist. Otherwise the size is unknown.
- Confirm large and unknown-size playlists before continuing. `--yes` confirms when playlist scope is selected. `--playlist` selects scope and still requires size confirmation. With interactive stdin, ask **Download the entire playlist? [y/N]** and show the known count, **at least 51 entries**, or **unknown size**. Declining ends successfully with a message that no downloads started. With non-interactive stdin and no `--yes`, exit `2` with the confirmation remedy.
- Reject auto-generated mixes, including YouTube `RD` playlist IDs, even with `--yes`. An inaccessible or empty playlist or failed metadata request exits `1` with a specific reason. After scope and any confirmation are settled, finish enumerating the ordered playlist before starting media downloads. Reapply the size rule if the completed listing reveals a large playlist that has yet to be confirmed. Give each metadata operation a 120-second deadline. If the full list cannot be obtained by then, fail before downloading any media.
- Use yt-dlp's `--yes-playlist` for playlist metadata and `--no-playlist` for each individual download, using the saved video ID. Keep the original positions in the job plan for entries that are unavailable or have no usable video ID. A `t=` parameter leaves the audio untrimmed.

## 6. File layout and configuration

```text
~/.local/bin/ydl                       # user-facing launcher
~/.local/share/ydl/bin/yt-dlp          # managed backend
~/.config/ydl/config.toml              # preferences
~/.local/state/ydl/jobs/<id>.json      # resumable job records
~/.local/state/ydl/logs/<id>.log       # detailed diagnostics
~/Downloads/ydl/                        # default completed downloads
```

Respect the corresponding `XDG_*_HOME` environment variables. These are the built-in values when no configuration is present.

```toml
output = "~/Downloads/ydl"
quality = "0"
keep_video = false
auto_update = true
channel = "nightly"
```

Name a new single download `Title.mp3` and a new playlist download `Playlist title/001 - Title.mp3`. Keep video IDs in the job plan and use the saved destination and stems on retry, including names with ID suffixes from earlier versions. Use the original one-based playlist position and pad shorter positions to three digits, including gaps for unavailable entries. Sanitize names and keep all generated paths within the resolved output directory. A retained video uses the same stem and the verified video's container extension.

For a new playlist job, create a fresh folder. If an entry already occupies the title-based path, try `Playlist title (2)`, `Playlist title (3)`, and so on until directory creation succeeds. Leave existing entries in place, including directories, files, and symlinks. When retrying, reject a symlink or other non-directory at the saved folder path.

An occupied file target is a collision unless it is a regular file matching the saved verification record for that job and item. A new job cannot reuse a file based on its name. For a collision, preserve the file, fail the affected item, name the path, and explain how to continue after moving the file or starting a new job with `--output`. Publish final files atomically with no overwrite, including when another process claims the path during the download.

## 7. Progress, cancellation and recovery

- Use `asyncio.create_subprocess_exec` for updater, metadata and download operations. Continuously drain stdout and stderr and emit structured progress events without blocking the event loop on subprocess I/O. Keep detailed subprocess output in the job log.
- When stderr is an interactive terminal that supports line clearing, use a single live row for preparation. Once the options, scope and destination are resolved and any consent prompts are complete, replace that row with a settings row and print it once with a newline. Show the resolved scope, MP3 quality or bitrate, video retention and destination. Use the row immediately below it for download and conversion progress across all items.
- Redraw the live row by moving to column one, clearing the row, and flushing the new text without a newline. Fit both rows within the terminal width, shortening the destination or title first. Treat metadata and paths as plain text so they cannot add rows or move the cursor.
- Show the current stage and item title, with playlist position, percentage, speed and ETA when known. A stage with indeterminate progress, such as conversion, shows its name and a spinner. Routine updates, including individual item completion, reuse the live row.
- Clear the preparation row before a prompt or a warning that needs to stay visible, then resume preparation. During downloads, show routine item failures in the live row and keep details in the job log. At completion, failure or cancellation, replace the live row with the final result and end it with a newline. Include success and failure counts where applicable, and the destination if the settings row was omitted. Print any remaining actionable diagnostics after the result.
- When stderr is redirected or the terminal lacks line-clearing support, emit plain-text prompts when applicable, actionable diagnostics and the final result. Omit the settings row, transient progress, spinners and cursor-control characters in this mode.
- Perform one media item at a time in v1. A playlist must continue after an individual recoverable item failure, with a clear end-of-run success/failure count.
- Ctrl+C must cancel the active subprocess tree cleanly (graceful stop, then forced stop after a bounded timeout), leave resumable partial downloads and job state intact, and exit with code `130`.
- Save the job plan atomically before downloading any media. Store its creation time, resolved settings and absolute destination, the original ordered list of entries, video IDs where available, and target filename stems. Save the retained video's exact filename once its container is known. `retry` always uses this plan and never enumerates the playlist again. Entries with an ID can be retried even if they were previously unavailable. An entry with no ID remains failed for that job. A new `ydl URL` invocation can enumerate it again. Allow only one active invocation of a given job. A concurrent retry for that job exits `1`.
- Use a staging directory on the destination filesystem, private to each job and item. Keep partial transfers there for yt-dlp's bounded retries and later resume. For a completed staged MP3, require download and conversion success plus `ffprobe` reporting an MP3 stream in an MP3 file. For retained video, require a usable video stream and an audio stream in the staged container. Remove staging sources only after all requested outputs are verified and published.
- Check target paths for collisions before recording and publishing new outputs. Save each verified staged output's final path and SHA-256 before publishing it so retry can recognize a valid result even if interrupted just after publication. On retry, skip a matching final file, regenerate a missing one, and report a collision for a path with different content or an unexpected file type. Keep a matching member of a partially published MP3/video pair and process only the missing output. Reuse the recorded video container when regenerating a missing video. If that container cannot be recreated without re-encoding the video, fail the item with an explanation. A job is complete only when all planned entries have all their verified outputs. An explicit retry of a completed job checks its saved files and succeeds without a download when they all match.
- Summarize failures in plain English and retain detailed stderr/log output. Classify at least: unavailable/private video, missing/unsupported runtime, missing FFmpeg, requested format absent, authentication/rate limit, network/update failure, conversion failure and destination permission failure. No infinite automatic retries.
- Exit `0` for successful commands, including an explicitly declined playlist confirmation. Exit `1` for operational failures, unavailable dependencies or failed items. Exit `2` for invalid arguments, configuration, job selection or missing non-interactive confirmation. Exit `130` for Ctrl+C.

## 8. Proposed implementation modules

```text
ydl/
  cli.py        # argparse, prompts, terminal rendering
  backend.py    # bootstrap, update, version, JS runtime, FFmpeg checks
  media.py      # URL classification, metadata, command construction
  jobs.py       # job state, resume, verified completion
  runner.py     # async job orchestration, progress events, cancellation
  config.py     # TOML preferences, XDG paths
```

Keep modules small and testable; only introduce abstractions needed by the above behavior. A console-script entry point named `ydl` should be installable into the user's account without root privileges.

### Future terminal UI

Expose the asynchronous download and retry workflow to frontends through `runner.py`. The runner takes the download request or retry selection, publishes structured events and returns a structured result. Events carry the resolved settings when ready and the stage, item ID, title and position, available transfer metrics, item outcomes and actionable diagnostics where applicable. Missing values remain absent. The result carries the overall outcome, completion counts and the destination. The frontend can cancel the workflow through the same cleanup path used for Ctrl+C.

Frontend code owns prompts and rendering. If the workflow needs a decision after discovering playlist metadata or a missing runtime, send the choices and any default to an asynchronous decision handler supplied by the frontend. Keep terminal input, rendering and process exit in `cli.py`. This lets a future TUI render the same events and present decisions in its own controls while reusing download, cancellation and retry behavior.

## 9. Acceptance criteria

1. **One-command happy path:** after user-level installation, `ydl URL` produces a playable MP3 in `~/Downloads/ydl` without requiring manual yt-dlp flags.
2. **Self-managed backend.** First use bootstraps yt-dlp without root. `--no-update` leaves the backend untouched and fails when it is missing. A failed automatic update preserves and uses the previous usable backend. A channel change applies on the next permitted update even when the target is older. `update --check` reports the difference without modifying the installation.
3. **Playlist scope and confirmation.** An ambiguous URL defaults to one video. A pure playlist with 50 entries starts without size confirmation. At 51 entries or with an unknown total, confirmation is required. `--playlist` and `--yes` follow the distinct selection and confirmation rules. A non-interactive run requiring confirmation exits `2` before downloading any media.
4. **Audio-only default:** no retained video and no full-video download when an audio-only format is available. MP3 uses the best available source audio and default VBR quality `0`.
5. **Video retention:** `--keep-video` yields both a valid MP3 and a playable video. Quality and output flags, plus persisted config, take effect.
6. **Live progress.** On a supported terminal, preparation uses one updating row. Downloading shows the resolved settings once and redraws one row beneath it throughout download, conversion and item transitions. A shorter update clears old text. Completion and Ctrl+C replace the progress row with a final result and leave a usable terminal prompt. Redirecting stderr selects plain-text mode.
7. **Cancellation, retry and conflicts.** Ctrl+C stops promptly and keeps resumable work. Retry uses the saved order and paths even after the source playlist changes. It skips verified outputs, including an output published just before interruption, and fills in missing outputs. A conflicting file survives unchanged and is reported as an item failure.
8. **Real errors:** missing JS runtime, unavailable audio format, missing FFmpeg and access-denied errors produce actionable diagnostics rather than a generic “download failed.”
9. **Test coverage:** focused unit tests for URL classification, flags, configuration, update fallback, progress events, cancellation and job reconciliation; integration tests use a fake yt-dlp/FFmpeg where practical. Run an optional real-YouTube smoke test separately from CI because YouTube availability changes. Release validation should explicitly exercise the previously failing `https://www.youtube.com/watch?v=vvpZOaQz6SM` link, verifying a playable MP3 (the full download may be time-consuming).
10. **No hidden infrastructure:** no Qt, Flatpak, Docker, administrator installation, shell evaluation, database or always-running service.

## 10. Explicitly out of scope for v1

A full-screen TUI, GUI, embedded player, media-library tagging, audio normalization, clipping by timestamp, parallel downloads, authentication-cookie automation, a detached daemon, video transcoding presets, and a generalized downloader for unrelated sites. These can be evaluated only if actual usage justifies them.

**Primary engineering rule:** prefer yt-dlp's established download/extraction logic; `ydl` should own the user experience, updates, lifecycle and reliability—not reimplement YouTube downloading.
