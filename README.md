# ydl

**A simple YouTube-to-MP3 downloader for Linux.**

Download individual videos or entire playlists as MP3 files. Use the interactive
terminal interface to queue multiple downloads, or the command line for quick
downloads.

- Download multiple videos and playlists, one at a time.
- Choose your MP3 quality and where to save your files.
- Optionally keep the original videos alongside the MP3s.
- Close and reopen the app without losing your download queue.
- Resume interrupted downloads and retry failed ones without re-downloading
  verified files.

By default, ydl saves MP3 files to `~/Downloads/ydl/`. New single-video downloads
use `Title.mp3`. Retrying an earlier job uses the filename saved with that job.

[Install](#install) · [Interactive mode](#interactive-mode) · [Command line](#command-line) · [Settings](#settings) · [Troubleshooting](#troubleshooting)

## Install

### Linux Mint and Ubuntu

You need Python 3.11 or newer. Check your version with `python3 --version`.

Install the required tools and download ydl:

```sh
sudo apt update
sudo apt install ffmpeg git
git clone https://github.com/stef-k/ydl.git
cd ydl
```

Install ydl with both the interactive interface and the command line:

```sh
sudo apt install pipx
pipx install '.[tui]'
pipx ensurepath
```

Open a new terminal and run `ydl tui` to get started.

If you only want the command line, use `pipx install .` instead.

<details>
<summary>Already installed ydl or need a different installation method?</summary>

To add the interactive interface to an existing pipx installation, run
`pipx install --force '.[tui]'` from the cloned repository.

If your compatible Python is installed as `python3.11`, use
`pipx install --python python3.11 '.[tui]'`.

If you prefer to install commands in `~/bin`, set
`export PIPX_BIN_DIR="$HOME/bin"` before installing with pipx. Make sure the
folder is on your PATH and keep the setting in your shell startup file.

If pipx is unavailable, you can install ydl in a Python virtual environment.
Run these commands from the cloned repository:

```sh
sudo apt install python3-venv
python3 -m venv "$HOME/.local/share/ydl-venv"
"$HOME/.local/share/ydl-venv/bin/python" -m pip install '.[tui]'
mkdir -p "$HOME/.local/bin"
ln -s "$HOME/.local/share/ydl-venv/bin/ydl" "$HOME/.local/bin/ydl"
```

Use `-m pip install .` for the command-line-only installation.

If `ydl` is not found, run `export PATH="$HOME/.local/bin:$PATH"` or use
`~/bin` if you installed it there. Add the appropriate line to your shell
startup file to make it permanent.

</details>

### First use

ydl automatically installs and updates its own copy of yt-dlp, the tool it uses
to download from YouTube. When you start a download, if neither Node.js 22+
nor Deno 2.3+ is available, ydl offers to install Deno. Choose **Yes** if you
want ydl to install it for you. No administrator privileges are needed for
these installations.

## Interactive mode

Open the app in your terminal:

```sh
ydl tui
```

Use your mouse or keyboard to manage downloads.

### Download videos and playlists

1. Paste one or more YouTube links, one per line.
2. Choose your MP3 quality, destination folder and whether to keep the video.
   Check **Remove from queue on success** to remove each entry in this batch
   after ydl verifies its saved outputs.
3. Click **Add to Queue**, review your links and choices, and click **Add**.
4. Click **Start queue** to begin downloading.

ydl processes the queue one entry at a time. **Active transfer** shows progress,
speed and estimated time remaining for the current item when available. A
playlist occupies one queue row, and Active transfer shows its current track.

The **Scope** option lets you choose between a single video and an entire
playlist. Leave it on **Auto** if you are unsure; ydl will ask when a link
contains both a video and a playlist. For playlists with more than 50 entries
or an unknown size, ydl asks for confirmation before downloading. YouTube
automatically generated mixes are not supported.

### Manage your downloads

- **Stop queue** stops the current work. Your queue is saved automatically.
  When you reopen the app, select **Start queue** or **Resume queue** when you return.
- **Retry failed** retries failed downloads.
- Scroll long queues with the mouse wheel, or focus the queue and use Up and Down.
- Use the menu next to a queue entry to move pending downloads, retry eligible
  jobs, recheck their status or remove them.
- Double-click a row in the queue or **Recent jobs** to open **Job Details**.
  In the **Progress** tab of a completed job, use **Open folder** to see the
  download directory. For a single-video job, use **Open audio** to play the MP3
  or **Open video** if you kept the video. These actions use your desktop's
  default apps through `gio`.
- **Recent jobs** shows up to 20 saved jobs, including downloads started from
  the command line and those cleared from the queue. You can also retry
  eligible unfinished jobs there.
- **Settings** changes the defaults for future queue entries.

**Remove from queue on success** waits for every track in a playlist to have
a verified MP3 and, if you kept the video, a verified video. The saved job
and its outputs remain available in **Recent jobs**. Use **Clear completed**
or **Remove selected** for completed entries that stay in the queue. Both
actions keep downloaded files, saved jobs and logs.

Use **Help** for keyboard shortcuts. Close any open dialog with **Close**, **Cancel**,
or **Esc** before using **Quit** or **Ctrl+Q** on the main screen.

## Command line

To download a video as MP3, replace the example link with a YouTube link:

```sh
ydl 'https://www.youtube.com/watch?v=VIDEO_ID'
```

Press Enter when asked whether to keep the original video to accept your
current preference (MP3 only by default). Keep the quotes around links.

### Download an entire playlist

```sh
ydl 'https://www.youtube.com/playlist?list=PLAYLIST_ID'
```

Playlist tracks are numbered, for example `001 - Title.mp3`, and saved in a
separate folder. If the link points to a video within a playlist, ydl asks
whether to download that video or the whole playlist.

### Common options

| Option | What it does |
| --- | --- |
| `--single` | Download only the selected video. |
| `--playlist` | Download the entire playlist (size confirmation still applies). |
| `--keep-video` | Keep the video as well as the MP3. |
| `--no-keep-video` | Save only the MP3. |
| `--quality 192K` | Set the audio bitrate to 192 kbps. |
| `--output "$HOME/Music"` | Save files to your Music folder. |
| `--yes` | Use the default choices unless overridden by options; also confirm large or unknown-size playlists when playlist mode is selected. |
| `--no-update` | Use the installed yt-dlp without installing or updating it. |

For variable bitrate (VBR), use `0` (highest and the default) to `9` (lowest). The `--yes` option does not bypass permission to install Deno.

For example, to save both the video and MP3 in your Music folder:

```sh
ydl 'https://www.youtube.com/watch?v=VIDEO_ID' --keep-video --output "$HOME/Music"
```

Run `ydl --help` for all available options.

## Settings

Change your preferences in the interactive app under **Settings**, or from the
command line using `ydl config set`.

| Setting | Default | How to change it |
| --- | --- | --- |
| Download folder | `~/Downloads/ydl/` | `ydl config set output "$HOME/Music"` |
| MP3 quality | `0` (highest VBR quality) | `ydl config set quality 192K` |
| Keep video | No | `ydl config set keep_video true` |
| Remove from queue on success | Off | TUI **Settings → General** |
| yt-dlp release channel | Nightly | `ydl config set channel stable` |

Run `ydl config show` to view your current settings. Command-line options
change the settings for that download only.

yt-dlp updates are checked automatically by default. You can change this in
**Settings**, or set `auto_update = false` in `~/.config/ydl/config.toml` (or
the corresponding XDG configuration directory).

## Troubleshooting

If a download is interrupted or fails, use **Resume queue** or **Retry failed**
in the interactive app, or one of these commands:

| Command | What it does |
| --- | --- |
| `ydl retry` | Retry the latest unfinished download job. |
| `ydl retry JOB_ID` | Retry a specific saved job using its ID. |
| `ydl doctor` | Check dependencies and directory permissions. |
| `ydl update` | Install or update yt-dlp. |
| `ydl update --check` | Check for an available yt-dlp update without installing it. |

If a queue entry shows **Needs attention**, open its details, fix the reported
problem and select **Recheck** from its menu. Two videos with the same title can
resolve to the same filename in one folder. ydl reports the collision and keeps
the existing file. Move it aside before retrying, or start a new download in a
different folder.

<details>
<summary>Queue recovery and log files</summary>

If the app reports a queue storage error, follow the message, then select
**Recover queue**. The queue is normally saved in
`~/.local/state/ydl/tui-queue.json` and logs in `~/.local/state/ydl/logs/`.
If `XDG_STATE_HOME` is set, ydl uses its `ydl/` subdirectory instead.

</details>

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[tui]'
.venv/bin/python -m unittest discover -s tests -v
```

See the [CLI specification](spec/ydl-cli-spec.md) and
[TUI specification](spec/ydl-tui-spec.md) for implementation details and the
live smoke test. Report problems on the [issue tracker](https://github.com/stef-k/ydl/issues).

## License

[MIT](LICENSE). Third-party tools retain their own licenses.
