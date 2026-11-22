# ydl

Download a YouTube video or playlist as MP3 on Linux. ydl manages a private
yt-dlp executable, calls FFmpeg for conversion, and can retry unfinished work.

## Install

Use Python 3.11 or newer and install FFmpeg and ffprobe through your Linux
distribution. YouTube also requires Deno 2.3+ or Node 22+ on your PATH. If
neither is available, ydl can install a private Deno after you approve.

From a local checkout of the private repository, install into your account.

```sh
pipx install .
ydl 'https://www.youtube.com/watch?v=VIDEO_ID'
```

You can also run `pip install .` inside a virtual environment. The first
download bootstraps yt-dlp. To check your prerequisites, run `ydl doctor`.

## Use

- MP3s go to `~/Downloads/ydl/` by default. ydl uses the best available
  audio-only source when possible and encodes at MP3 VBR quality `0`.
  Choose `--quality 192K` for a fixed bitrate.
- `--keep-video` saves the playable video alongside the MP3.
- A video link with playlist context defaults to the video. Use `--playlist`
  to select the full playlist. Playlists over 50 entries or of unknown size
  need confirmation. `--yes` accepts the configured defaults and confirms
  playlist size if playlist scope was selected.
- The terminal shows the resolved settings above one in-place progress row.
  Press Ctrl+C to stop and use `ydl retry` or `ydl retry JOB_ID` to continue.
  Retry uses the original entries and preserves verified outputs.
- Use `--output DIRECTORY` for this run or `ydl config set output DIRECTORY`
  to save a new default. `ydl config show` shows the effective configuration.
- The private backend checks the nightly channel by default. Use
  `ydl config set channel stable` to switch, `ydl update --check` to compare
  releases, or `--no-update` to use your installed backend for a download.

Config, data, and job state follow the XDG base directories. By default,
preferences are in `~/.config/ydl/config.toml`, the backend is under
`~/.local/share/ydl/`, and job records and diagnostics are under
`~/.local/state/ydl/`.

See the [CLI specification](ydl-cli-spec.md) for the full behavior and the
[changelog](CHANGELOG.md) for changes.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

CI runs the tests without downloading from YouTube. To validate live service
compatibility before a release, use the smoke-test URL in the specification.

ydl is available under the [MIT License](LICENSE). Installed third-party tools
retain their own licenses.
