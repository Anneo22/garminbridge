#!/usr/bin/env python3
"""test_locations.py: OFFLINE proof of the saved-points byte surgery against the real
Mac-backup Lctns.fit. Touches NOTHING on the watch and never writes the backup: every edit
runs on an in-memory copy and is re-parsed + CRC-checked + (if fitdecode is present)
decoded by a real FIT parser. This is the gate before any hardware test."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import locations as L

SRC = None
RAW = None

try:
    import fitdecode
    HAVE_FD = True
except Exception:
    HAVE_FD = False


def fitdecode_ok(data, tag):
    """Round-trip the edited bytes through a real FIT parser (fitdecode) and count location
    messages. Proves it's not just our own parser that accepts the file."""
    if not HAVE_FD:
        return None
    import io
    n = 0
    with fitdecode.FitReader(io.BytesIO(bytes(data))) as fr:
        for f in fr:
            if isinstance(f, fitdecode.FitDataMessage) and f.global_mesg_num == L.LOC_MSG:
                n += 1
    print(f"    fitdecode({tag}): {n} location messages, no parse error")
    return n


def main():
    global SRC, RAW
    SRC = L.mac_backup_path()
    RAW = open(SRC, "rb").read()

    fails = []

    def check(cond, msg):
        print(("  ok   " if cond else "  FAIL ") + msg)
        if not cond:
            fails.append(msg)

    print(f"source: {SRC} ({len(RAW)} bytes)")
    print("\n[1] LIST (baseline)")
    pts = L.points_from_bytes(RAW)
    for p in pts:
        print(f"    #{p['index']}  {p['name']!r}  ({p['lat']}, {p['lon']})")
    check(len(pts) == 5, "5 saved points parsed")
    check([p["index"] for p in pts] == [0, 1, 2, 3, 4], "message_index is 0..4 contiguous")
    check(L._crcs_ok(bytearray(RAW)), "original CRCs verify")
    slot = L.name_slot(bytearray(RAW))
    check(slot == 32, f"name slot is 32 bytes (got {slot})")
    fitdecode_ok(RAW, "orig")

    print("\n[2] RENAME index 2 -> 'IVC Gym' (fixed slot, length identical)")
    new = L.rename_bytes(RAW, 2, "IVC Gym")
    check(len(new) == len(RAW), f"file length unchanged ({len(new)})")
    rp = L.points_from_bytes(new)
    check(rp[2]["name"] == "IVC Gym", "index 2 renamed to 'IVC Gym'")
    check([p["name"] for i, p in enumerate(rp) if i != 2] ==
          [p["name"] for i, p in enumerate(pts) if i != 2], "all other names unchanged")
    check([p["index"] for p in rp] == [0, 1, 2, 3, 4], "indices preserved")
    check(L._crcs_ok(bytearray(new)), "CRCs valid after rename")
    L.verify(new, 5)
    fitdecode_ok(new, "rename")

    print("\n[3] RENAME with a 33-byte name must refuse")
    try:
        L.rename_bytes(RAW, 0, "x" * 33)
        check(False, "33-byte name rejected")
    except ValueError as e:
        check("name_too_long" in str(e), f"33-byte name rejected ({e})")
    # a full 32-byte name is allowed (fills the slot exactly, no terminator)
    edge = L.rename_bytes(RAW, 0, "y" * 32)
    check(L.points_from_bytes(edge)[0]["name"] == "y" * 32, "32-byte name fills the slot")
    check(L._crcs_ok(bytearray(edge)), "CRCs valid for the 32-byte edge case")

    print("\n[4] DELETE each index -> 4 points, survivors renumbered 0..3")
    for di in range(5):
        d = L.delete_bytes(RAW, di)
        dp = L.points_from_bytes(d)
        expect_names = [p["name"] for p in pts if p["index"] != di]
        check(len(d) == len(RAW) - 74, f"delete #{di}: length {len(RAW)} -> {len(d)} (-74)")
        check([p["name"] for p in dp] == expect_names,
              f"delete #{di}: survivors are {expect_names}")
        check([p["index"] for p in dp] == [0, 1, 2, 3],
              f"delete #{di}: survivors renumbered 0..3")
        check(L._data_size(d) == len(d) - d[0] - 2, f"delete #{di}: header data_size consistent")
        check(L._crcs_ok(bytearray(d)), f"delete #{di}: both CRCs valid")
        L.verify(d, 4)
        fitdecode_ok(d, f"del{di}")

    print("\n[5] DELETE then RENAME a survivor (compound, exercises renumbered handles)")
    d = L.delete_bytes(RAW, 1)           # drop 'Home' -> survivors 0..3 = Abbey pool,IVC,DD,Latham
    d2 = L.rename_bytes(d, 2, "DD2")     # index 2 is now 'DD'
    dp = L.points_from_bytes(d2)
    check([p["name"] for p in dp] == ["Abbey pool", "IVC", "DD2", "12 Latham Road, Cambridge, CB2 "],
          "compound delete+rename yields the expected list")
    check(L._crcs_ok(bytearray(d2)), "CRCs valid after compound edit")
    L.verify(d2, 4)

    print("\n[6] source file untouched by any of the above")
    check(open(SRC, "rb").read() == RAW, "Mac backup on disk is byte-identical (no writes)")

    print(f"\n{'ALL PASS' if not fails else str(len(fails)) + ' FAILURES'}"
          + (" (fitdecode not installed; skipped real-parser checks)" if not HAVE_FD else ""))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
