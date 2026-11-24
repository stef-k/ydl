# ydl

**Save a YouTube video or an entire playlist as MP3 from your Linux terminal.**
Use the interactive terminal user interface (TUI) to choose download options,
manage a persistent download queue, and view progress. Use the command-line
interface (CLI) to run downloads directly from the shell. Both save files to
`~/Downloads/ydl/` by default and can keep the video or resume interrupted work.

[Install](#install-on-linux-mint-or-ubuntu) · [Use the TUI](#use-the-tui) · [Use the CLI](#use-the-cli) · [CLI switches](#common-switches) · [Defaults](#save-your-defaults)

## Install on Linux Mint or Ubuntu

### 1. Check Python and install FFmpeg and Git

You need Python 3.11 or newer. The `ffmpeg` package provides both FFmpeg and
FFprobe.

```sh
python3 --version
sudo apt update
sudo apt install ffmpeg git
```

### 2. Get the source

```sh
git clone https://github.com/stef-k/ydl.git
cd ydl
```

### 3. Install ydl with pipx

Install `pipx` if you need it.

```sh
sudo apt install pipx
```

Run one of these install commands from the cloned `ydl` folder.

| Interface | Install | Use |
| --- | --- | --- |
| Interactive TUI with download queue | `pipx install '.[tui]'` | `ydl tui` |
| Command line only | `pipx install .` | `ydl URL` |

The TUI installation also includes the CLI. If you already installed ydl with
pipx, run `pipx install --force '.[tui]'` to add the TUI to that installation.

By default, pipx puts the `ydl` command in `~/.local/bin`. If you use `~/bin`
for your own commands, run `export PIPX_BIN_DIR="$HOME/bin"` before installing
ydl. Keep that export in your shell startup file for future pipx installs.

If your newer Python is installed under a separate name, add
`--python python3.11` to the install command. If your `apt` doesn't provide
`pipx`, use the virtual environment instructions below.

Run `pipx ensurepath`, then open a new terminal and check that `ydl` works from
any directory.

```sh
command -v ydl
ydl --help
```

To use `ydl` immediately in your current terminal, run
`export PATH="$HOME/.local/bin:$PATH"`. Use `export PATH="$HOME/bin:$PATH"`
if you installed the command there.

<details>
<summary>Install using a virtual environment instead of pipx</summary>

From the checkout, install into a virtual environment in your home directory
and link the command into `~/.local/bin`.

```sh
sudo apt install python3-venv
python3 -m venv "$HOME/.local/share/ydl-venv"
"$HOME/.local/share/ydl-venv/bin/python" -m pip install .
mkdir -p "$HOME/.local/bin"
ln -s "$HOME/.local/share/ydl-venv/bin/ydl" "$HOME/.local/bin/ydl"
```

To install the TUI and CLI together, use `-m pip install '.[tui]'` for the install step.

If `~/bin` is already on your PATH, you can use it in the last two commands
instead. If you use `~/.local/bin` and it is missing from your PATH, run
`export PATH="$HOME/.local/bin:$PATH"`. Add that line to `~/.bashrc` to keep
it in future interactive Bash sessions.

</details>

### First-run setup

`ydl` installs and manages its own copy of yt-dlp in your home directory.
yt-dlp uses **Node.js 22+** or **Deno 2.3+** to get media links from YouTube's
player. If you already have a compatible runtime on your PATH, `ydl` uses it.
Otherwise, answer **Yes** at the Deno prompt to install a copy for your account.

## Use the TUI

After installing with the `tui` extra, open the interactive interface in your
terminal. You can start with an empty editor or prefill a YouTube link.

```sh
ydl tui
# Or open with a link already in the editor
ydl tui 'https://www.youtube.com/watch?v=VIDEO_ID'
```

### Add links and start downloading

1. Paste one YouTube link per line. Choose **Scope**, **MP3 quality**, and whether
   to **Keep video**. Type a folder in **Save to** or select it with **Browse**.
2. Select **Add to Queue**. Review or edit the links in **Add URLs**, then
   select **Add**.
3. Select **Start queue**. The queue runs one entry at a time and shows the
   current track and available progress, speed, and time estimates in
   **Active transfer**.

**Auto** scope downloads a playlist-only link as a playlist. For a link to a
video inside a playlist, it opens a chooser with **This video** selected.
Choose **Entire playlist** to download the full list. Playlists with 51 or more
entries or an unknown size ask for confirmation.

### Manage the queue and saved jobs

- Use **Stop queue** or **Ctrl+C** to stop. The queue reopens idle with its
  saved entries. Select **Resume queue** to continue an interrupted job.
- Open a row’s **⋮** menu, or focus the queue and press **F2**, to move pending
  entries, recheck a job, or remove an entry. Removing rows and using
  **Clear completed** preserve the media, saved jobs, and logs.
- Double-click a row or select it and press **Enter** to open **Job Details**.
  The **Log** tab shows what happened, and **Info** shows the saved destination.
  If a row shows **Needs attention**, read the diagnostic, fix the problem, and
  choose **Recheck** from its row menu.
- Use **Retry failed** to attempt failed rows in queue order, or retry one row
  from its menu. **Recent jobs** gives access to saved jobs, including eligible
  jobs started from the command line.

Open **Settings** to change defaults for future entries and manage yt-dlp
updates. Use **Tab** and **Shift+Tab** to move focus, arrow keys to navigate,
and **Enter** to activate a focused control. Open **Help** for the full list
of shortcuts. Use **Quit** or **Ctrl+Q** to close the app.

If a queue storage error appears, scroll the notice to read it, address the
error, and choose **Recover queue**. The queue file is usually at
`~/.local/state/ydl/tui-queue.json`, or under `$XDG_STATE_HOME/ydl/` if set.

## Use the CLI

### Download your first MP3

Replace the example link with a YouTube link. Keep the quotes, especially
when the link contains `&`.

```sh
ydl 'https://www.youtube.com/watch?v=VIDEO_ID'
```

Press Enter at **Keep original video file(s)?** to accept the default of
saving only an MP3. The settings stay on one line while the line below updates
with download and conversion progress. The final summary tells you how many
files were saved.

#### Download a playlist

```sh
ydl 'https://www.youtube.com/playlist?list=PLAYLIST_ID'
ydl 'https://www.youtube.com/watch?v=VIDEO_ID&list=PLAYLIST_ID' --playlist
```

A playlist-only link selects the whole playlist. A link that points to a
specific video inside a playlist defaults to **This video**. Use
`--playlist` to select the whole list. Playlists with **51 or more entries**
or an unknown size ask for confirmation before downloading. Playlist tracks
are numbered in a subfolder of the output directory.

### Common switches

| Switch | What it does |
| --- | --- |
| `--single` | Download only the video named in the link. |
| `--playlist` | Select the entire playlist. The size confirmation still applies. |
| `--yes` | Use the default scope and video-retention preference unless you pass explicit flags. If playlist scope is selected, also confirm a large or unknown-size playlist. JavaScript runtime installation still asks for permission. |
| `--keep-video` / `--no-keep-video` | Choose whether to save the playable video alongside the MP3 for this run. |
| `--quality 192K` | Set a fixed MP3 bitrate. For variable bitrate, use `0` through `9`, with `0` the highest quality and the default. |
| `--output "$HOME/Music"` | Save this run to a different folder. |
| `--no-update` | Use the already installed yt-dlp and skip its update check for this run. |

For example, to save both files in Music, run
`ydl 'https://www.youtube.com/watch?v=VIDEO_ID' --keep-video --output "$HOME/Music"`.

### Save your defaults

See your current settings with `ydl config show`. Settings apply to future
downloads. A flag on the command line takes precedence for that run.

| Setting | Built-in default | Example change |
| --- | --- | --- |
| Download folder | `~/Downloads/ydl/` | `ydl config set output "$HOME/Music"` |
| MP3 quality | Variable bitrate, quality `0` | `ydl config set quality 192K` |
| Keep the original video | Off | `ydl config set keep_video true` |
| yt-dlp release channel | `nightly` | `ydl config set channel stable` |

Automatic yt-dlp updates are enabled by default. To turn off automatic update
checks for an existing backend, set `auto_update = false` in
`~/.config/ydl/config.toml` or the corresponding XDG config location.

### Retry, diagnose, or update

| Command | When to use it |
| --- | --- |
| `ydl retry` | Continue the latest unfinished job after a failure or Ctrl+C. Completed files are kept. |
| `ydl retry JOB_ID` | Retry a specific saved job using the ID shown when it stopped or failed. |
| `ydl doctor` | Check FFmpeg, yt-dlp, the JavaScript runtime, and directory permissions. |
| `ydl update` | Install or update the private yt-dlp executable now. |
| `ydl update --check` | See the installed yt-dlp version and whether an install or update is available. |
| `ydl --help` | Show download syntax and list other commands. |

If a filename is already occupied, `ydl` leaves the file in place. Move it
aside before retrying, or start a new download with a different `--output`.
Job logs are in `~/.local/state/ydl/logs/` by default.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[tui]'
.venv/bin/python -m unittest discover -s tests -v
```

CI tests the base and TUI installations separately without downloading from
YouTube. For the live smoke test and acceptance cases, see the
[TUI specification](spec/ydl-tui-spec.md). The [CLI specification](spec/ydl-cli-spec.md)
describes the command-line workflow.

ydl is available under the [MIT License](LICENSE). Third-party tools retain
their own licenses.
