# ydl

**Save a YouTube video or an entire playlist as MP3 from your Linux terminal.**
Give `ydl` a YouTube link and it downloads and converts the audio for you.
Files go to `~/Downloads/ydl/` by default. You can also keep the video or
resume an interrupted download.

[Install](#install-on-linux-mint-or-ubuntu) · [First download](#download-your-first-mp3) · [Switches](#common-switches) · [Defaults](#save-your-defaults)

## Install on Linux Mint or Ubuntu

### 1. Check Python and install FFmpeg

You need Python 3.11 or newer. The `ffmpeg` package provides both FFmpeg and
FFprobe.

```sh
python3 --version
sudo apt update
sudo apt install ffmpeg
```

### 2. Get the source

This is a private repository. Use a GitHub account with access. If you have
the GitHub CLI, sign in with `gh auth login` if needed, then run the following.

```sh
gh repo clone stef-k/ydl
cd ydl
```

With GitHub SSH access, you can also use
`git clone git@github.com:stef-k/ydl.git` followed by `cd ydl`.

### 3. Install the command with pipx

Install `pipx` and run the following from the `ydl` checkout. By default
`pipx` puts the `ydl` command in `~/.local/bin`. If you use `~/bin` for your
own commands, run `export PIPX_BIN_DIR="$HOME/bin"` before `pipx install .`.
Keep that export in your shell startup file to use `~/bin` for future pipx
commands.

```sh
sudo apt install pipx
pipx install .
pipx ensurepath
```

If your newer Python is installed under a separate name, use a command such
as `pipx install --python python3.11 .` for the install step. If your `apt`
doesn't provide `pipx`, use the virtual environment instructions below.

Open a new terminal to load the PATH change, then check that the command works
from any directory.

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

If `~/bin` is already on your PATH, you can use it in the last two commands
instead. If you use `~/.local/bin` and it is missing from your PATH, run
`export PATH="$HOME/.local/bin:$PATH"`. Add that line to `~/.bashrc` to keep
it in future interactive Bash sessions.

</details>

## Use the terminal queue

From the checkout, install the interactive frontend with `pipx install '.[tui]'`.
If ydl is already installed with pipx, use `pipx install --force '.[tui]'`.
Then run `ydl tui` in a terminal. You can also pass a URL to prefill the draft.

1. Paste one YouTube link per line, choose the quality and destination, then
   use **Add to Queue** and review the batch.
2. Select **Start queue**. For links with both a video and a playlist, Auto
   opens a scope chooser with **This video** selected.
3. Use **Stop queue** or **Ctrl+C** to interrupt work. Reopen with `ydl tui`
   and use **Resume queue** to continue in saved order.

Open a row’s **⋮** menu or press **F2** on the queue to reorder pending entries,
recheck saved work, or view job details and logs. **Retry failed** attempts failed
entries once in queue order. **Help** lists the keyboard shortcuts.

Keep the output directory in place during downloads. The queue is stored in
`$XDG_STATE_HOME/ydl/tui-queue.json` (usually `~/.local/state/ydl/tui-queue.json`).
If a queue save needs recovery, use **Recover queue** after addressing the error.
Removing a queue entry preserves its output and saved job files.

## Download your first MP3

Replace the example link with a YouTube link. Keep the quotes, especially
when the link contains `&`.

```sh
ydl 'https://www.youtube.com/watch?v=VIDEO_ID'
```

On your first download, `ydl` installs its own copy of yt-dlp in your home
directory. yt-dlp's bundled solver uses **Node.js 22+** or **Deno 2.3+** to
get media links from YouTube's player. If you already have a compatible
runtime on your PATH, `ydl` uses it. Otherwise, answer **Yes** at the Deno
prompt to install a private copy for your account.

Press Enter at **Keep original video file(s)?** to accept the default of
saving only an MP3. The settings stay on one line while the line below updates
with download and conversion progress. The final summary tells you how many
files were saved.

### Download a playlist

```sh
ydl 'https://www.youtube.com/playlist?list=PLAYLIST_ID'
ydl 'https://www.youtube.com/watch?v=VIDEO_ID&list=PLAYLIST_ID' --playlist
```

A playlist-only link selects the whole playlist. A link that points to a
specific video inside a playlist defaults to **This video**. Use
`--playlist` to select the whole list. Playlists with **51 or more entries**
or an unknown size ask for confirmation before downloading. Playlist tracks
are numbered in a subfolder of the output directory.

## Common switches

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

## Save your defaults

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

## Retry, diagnose, or update

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
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

CI runs the tests without downloading from YouTube. See the
[CLI specification](spec/ydl-cli-spec.md) for the complete behavior and the live
smoke test, and the [changelog](CHANGELOG.md) for changes.

ydl is available under the [MIT License](LICENSE). Third-party tools retain
their own licenses.
