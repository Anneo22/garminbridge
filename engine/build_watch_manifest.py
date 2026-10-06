#!/usr/bin/env python3
"""build_watch_manifest.py — read the INTERNAL name out of each pulled watch FIT and
cache it, so content.py can match watch files to Connect by their real name instead
of Garmin's lossy ~30-char filename (kills the truncation false-negatives). Run this
while the watch is connected (after `watch_mtp.sh get-all <leaf> <dir>`); the manifest
then lets the matrix match correctly OFFLINE.

  ./build_watch_manifest.py course <pulled_dir>
  ./build_watch_manifest.py workout <pulled_dir>

Writes/merges watch_manifest.json = { "<kind>": { "<filename>": "<internal name>" } }.
"""
import json
import os
import sys

import fitdecode
import paths

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = paths.state("watch_manifest.json")
# (fit message name, field holding the human name) per content kind
SOURCE = {"course": ("course", "name"), "workout": ("workout", "wkt_name")}


def internal_name(path, msg, field):
    try:
        with fitdecode.FitReader(open(path, "rb")) as fr:
            for frame in fr:
                if isinstance(frame, fitdecode.FitDataMessage) and frame.name == msg:
                    try:
                        v = frame.get_value(field)
                        if v:
                            return v
                    except Exception:
                        pass
    except Exception as e:
        sys.stderr.write(f"  decode failed {os.path.basename(path)}: {e!r}\n")
    return None


def main():
    if len(sys.argv) < 3:
        raise SystemExit("usage: build_watch_manifest.py <course|workout> <pulled_dir>")
    kind, src = sys.argv[1], sys.argv[2]
    msg, field = SOURCE[kind]

    out = {}
    for f in sorted(os.listdir(src)):
        if f.lower().endswith(".fit"):
            out[f] = internal_name(os.path.join(src, f), msg, field)

    manifest = {}
    if os.path.exists(MANIFEST):
        manifest = json.load(open(MANIFEST))
    manifest[kind] = {**manifest.get(kind, {}), **out}  # merge, so multiple pulls accumulate
    json.dump(manifest, open(MANIFEST, "w"), indent=2, ensure_ascii=False)

    named = sum(1 for v in out.values() if v)
    print(f"{kind}: {len(out)} files, {named} with an internal name -> {MANIFEST}")
    for f, nm in out.items():
        print(f"  {f[:40]:40} -> {nm!r}")


if __name__ == "__main__":
    main()
