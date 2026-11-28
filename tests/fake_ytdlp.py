"""Offline yt-dlp stand-in used by the installed CLI tests."""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

args = sys.argv[1:]
if "--version" in args:
    print("2026.01.01")
    sys.exit(0)

scenario = Path(os.environ["YDL_TEST_SCENARIO"])
video = parse_qs(urlsplit(args[-1]).query).get("v", [""])[0]
settings = json.loads((scenario / "scenario.json").read_text())
with (scenario / "calls").open("a") as calls:
    calls.write(json.dumps(args) + "\n")

if "--dump-single-json" in args:
    if "--yes-playlist" not in args:
        print(json.dumps({"id": video, "title": settings.get("title", "Sample song")}))
        sys.exit(0)
    info = {"title": "Test playlist", "entries": settings["entries"]}
    if "--playlist-end" in args:
        cap = int(args[args.index("--playlist-end") + 1])
        info["entries"] = info["entries"][:cap]
        info.update(settings.get("probe", {}))
    print(json.dumps(info))
    sys.exit(0)

format_selection = args[args.index("-f") + 1]
target = Path(args[args.index("-o") + 1])
if video in settings.get("fail", []):
    print("ERROR: This is a private video", file=sys.stderr)
    sys.exit(1)
if settings.get("hang"):
    Path(str(target).replace("%(ext)s", "wav.part")).write_text("partial")
    child = subprocess.Popen([sys.executable, "-c", settings["child"]])
    (scenario / "child_pid").write_text(str(child.pid))
    time.sleep(60)
    sys.exit(1)
default_source = "source.mkv" if format_selection == "bv*+ba/b" else "source.wav"
source = scenario / settings.get("source", default_source)
target = target.with_suffix("").with_name("source" + source.suffix)
shutil.copyfile(source, target)
print('ydl:download:{"status":"downloading","downloaded_bytes":68,"total_bytes":100,"speed":1024,"eta":12}')
print('ydl:postprocess:{"status":"started"}')
print("ydl:path:" + json.dumps(str(target)))
if any(arg.startswith("after_move:ydl:metadata:") for arg in args):
    sys.stdout.flush()
    for record in settings.get("transfer_records", {}).get(video, []):
        payload = record if isinstance(record, str) else json.dumps(record)
        sys.stdout.buffer.write(f"ydl:metadata:{payload}\n".encode("utf-8", errors="surrogateescape"))
