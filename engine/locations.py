#!/usr/bin/env python3
"""locations.py: GarminBridge saved-points (Lctns.fit) manager, the last content type.

Saved points ("locations") are unlike workouts/courses: they do NOT live in Garmin Connect
and are NOT one-file-per-item. Every saved point on a watch lives in ONE consolidated FIT,
GARMIN/Locations/Lctns.fit (mirrored to the Mac backup), each a fixed 74-byte `location`
record (global msg 29) sharing a single definition. So editing is byte surgery on that one
file, never a re-encode: fit_tool silently drops Garmin-proprietary fields (the same lesson
as fit_name_patch), which could strand a broken saved-points file on the watch.

Operations, each keeping every unrelated byte identical:
  rename  overwrite one record's FIXED 32-byte name slot + recompute the file CRC.
  delete  splice the 74-byte record, shrink the header data_size, renumber the survivors'
          message_index to a contiguous 0..N-1 (how the watch writes it natively), and
          recompute BOTH the 14-byte-header CRC (bytes 12-13) AND the trailing file CRC.

Record layout (global 29, verified 2026-07-01): 253 timestamp(4) · 0 name(32) ·
1 position_lat(4, semicircles) · 2 position_long(4) · 18(16) · 254 message_index(2, the
stable per-point handle) · 3(2) · 4(2) · 5(2) · 6(1) · 17(1) · 19(1) · 20(1) · 21(1).

Watch write mirrors the course swap and adds an optimistic-concurrency guard: pull the LIVE
file off the watch (never trust a possibly stale backup), patch a copy, decode-verify, refuse
if the target point drifted (index gone or name changed since the UI listed it), then
delete+upload and sync the Mac backup. Reads (list/preview) use the fast offline Mac backup.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

from fit_name_patch import fit_crc, _u16
import paths

HERE = os.path.dirname(os.path.abspath(__file__))
MTP = paths.asset("watch_mtp.sh")
WATCH_LEAF = "GARMIN/Location"   # SINGULAR on the watch (verified on-watch 2026-07-01); the
# Mac backup folder is "Locations" (plural), an asymmetry, so MAC_BACKUP keeps its own path.
WATCH_FNAME = "Lctns.fit"


def mac_backup_path():
    """Path to the Mac saved-points backup Lctns.fit (under GVE_SET_DEST)."""
    return os.path.join(paths.bridge_settings_dir(), "Locations", "Lctns.fit")

LOC_MSG = 29
F_NAME, F_LAT, F_LON, F_MIDX = 0, 1, 2, 254
SEMI = 180.0 / 2 ** 31
INVALID = 0x7FFFFFFF   # FIT sint32 "no value"


# ---- structural parse ---------------------------------------------------------

def _walk(data):
    """Pure structural FIT parse. Returns (hdr_size, data_end, records) where each record is
    {gmsg, start, end, fields:{fnum:(offset,size)}} for every DATA message. Tracks the active
    definition per local type; never interprets a field value."""
    hdr = data[0]
    if hdr not in (12, 14):
        raise ValueError(f"unexpected FIT header size {hdr}")
    data_end = len(data) - 2  # last 2 bytes = file CRC
    defs, records, pos = {}, [], hdr
    while pos < data_end:
        rec = data[pos]
        if rec & 0x80:
            raise ValueError("compressed-timestamp header not supported")
        local = rec & 0x0F
        if rec & 0x40:  # definition message
            base = pos + 1
            arch = data[base + 1]                 # 0 = little-endian, 1 = big-endian
            gmsg = ((data[base + 2] << 8) | data[base + 3]) if arch else _u16(data, base + 2)
            nfields = data[base + 4]
            p = base + 5
            fields = []
            for _ in range(nfields):
                fields.append((data[p], data[p + 1]))  # (field_def_num, size)
                p += 3
            devsz = []
            if rec & 0x20:  # developer fields present
                ndev = data[p]; p += 1
                for _ in range(ndev):
                    devsz.append(data[p + 1]); p += 3
            defs[local] = (gmsg, fields, devsz)
            pos = p
        else:  # data message
            gmsg, fields, devsz = defs[local]
            start, off, fmap = pos, pos + 1, {}
            for fnum, fsize in fields:
                fmap[fnum] = (off, fsize)
                off += fsize
            for dsz in devsz:
                off += dsz
            records.append({"gmsg": gmsg, "start": start, "end": off, "fields": fmap})
            pos = off
    return hdr, data_end, records


def _loc_records(data):
    """The location (global 29) records, each with its decoded fields + byte offsets so the
    same list drives both display and in-place edits."""
    _, _, records = _walk(data)
    out = []
    for r in records:
        if r["gmsg"] != LOC_MSG:
            continue
        fm = r["fields"]
        name = ""
        if F_NAME in fm:
            o, s = fm[F_NAME]
            name = bytes(data[o:o + s]).split(b"\x00", 1)[0].decode("utf-8", "replace")
        midx = int.from_bytes(data[fm[F_MIDX][0]:fm[F_MIDX][0] + fm[F_MIDX][1]], "little") \
            if F_MIDX in fm else None

        def _deg(fnum):
            if fnum not in fm:
                return None
            o, s = fm[fnum]
            v = int.from_bytes(data[o:o + s], "little", signed=True)
            return round(v * SEMI, 6) if v != INVALID else None

        out.append({
            "index": midx, "name": name, "lat": _deg(F_LAT), "lon": _deg(F_LON),
            "start": r["start"], "end": r["end"],
            "name_off": fm[F_NAME][0] if F_NAME in fm else None,
            "name_size": fm[F_NAME][1] if F_NAME in fm else None,
            "midx_off": fm[F_MIDX][0] if F_MIDX in fm else None,
            "midx_size": fm[F_MIDX][1] if F_MIDX in fm else None,
        })
    return out


def points_from_bytes(data):
    """Public saved-point list: [{index, name, lat, lon}] (no byte offsets)."""
    return [{"index": p["index"], "name": p["name"], "lat": p["lat"], "lon": p["lon"]}
            for p in _loc_records(bytearray(data))]


def points_from_backup(path=None):
    """The offline list the UI shows, from the Mac backup. Returns [] if there's no backup."""
    if path is None:
        path = mac_backup_path()
    if not os.path.exists(path):
        return []
    return points_from_bytes(open(path, "rb").read())


def name_slot(data):
    """The byte size of the location name field (the rename ceiling). Fixed 32 in practice, but
    read from the definition so the limit is always the file's real allocation."""
    recs = _loc_records(bytearray(data))
    return recs[0]["name_size"] if recs and recs[0]["name_size"] else None


# ---- CRC + verification -------------------------------------------------------

def _fix_crcs(data):
    """Recompute the header CRC (14-byte header only, bytes 12-13 over bytes 0..11) and the
    trailing file CRC (over all but the last 2 bytes). Mutates in place."""
    if data[0] == 14:
        hc = fit_crc(data[:12])
        data[12] = hc & 0xFF
        data[13] = (hc >> 8) & 0xFF
    end = len(data) - 2
    fc = fit_crc(data[:end])
    data[end] = fc & 0xFF
    data[end + 1] = (fc >> 8) & 0xFF


def _crcs_ok(data):
    if data[0] == 14 and _u16(data, 12) != fit_crc(data[:12]):
        return False
    end = len(data) - 2
    return _u16(data, end) == fit_crc(data[:end])


def _data_size(data):
    return data[4] | (data[5] << 8) | (data[6] << 16) | (data[7] << 24)


def verify(data, expect_count):
    """Decode-gate an edited buffer: CRCs valid, header data_size consistent with the actual
    length, re-parses cleanly, and carries exactly `expect_count` saved points. Returns the
    public point list; raises ValueError on any failure."""
    data = bytearray(data)
    if not _crcs_ok(data):
        raise ValueError("crc check failed after edit")
    if _data_size(data) != len(data) - data[0] - 2:
        raise ValueError("header data_size does not match file length after edit")
    pts = points_from_bytes(data)
    if len(pts) != expect_count:
        raise ValueError(f"expected {expect_count} saved points after edit, got {len(pts)}")
    return pts


# ---- edits (return a NEW buffer; never mutate the source) ---------------------

def rename_bytes(data, index, new_name):
    """New buffer with the saved point at message_index `index` renamed. Byte-length identical
    (fixed 32-byte slot, null-padded); only the name slot + file CRC change. Raises on a missing
    index or a name too long for the slot."""
    data = bytearray(data)
    p = next((p for p in _loc_records(data) if p["index"] == index), None)
    if not p:
        raise ValueError(f"no saved point with index {index}")
    enc = new_name.encode("utf-8")
    if p["name_size"] is None:
        raise ValueError("saved point has no name field")
    if len(enc) > p["name_size"]:
        raise ValueError(f"name_too_long: {len(enc)} bytes > {p['name_size']}-byte slot")
    o, s = p["name_off"], p["name_size"]
    data[o:o + s] = enc + b"\x00" * (s - len(enc))
    _fix_crcs(data)
    return data


def delete_bytes(data, index):
    """New buffer with the saved point at message_index `index` removed: splice the record,
    shrink the header data_size, renumber the survivors' message_index contiguously 0..N-1 (in
    file order, matching a native watch write), and recompute both CRCs. Raises on a missing
    index."""
    data = bytearray(data)
    tgt = next((p for p in _loc_records(data) if p["index"] == index), None)
    if not tgt:
        raise ValueError(f"no saved point with index {index}")
    rec_len = tgt["end"] - tgt["start"]
    del data[tgt["start"]:tgt["end"]]
    ds = _data_size(data) - rec_len
    data[4] = ds & 0xFF
    data[5] = (ds >> 8) & 0xFF
    data[6] = (ds >> 16) & 0xFF
    data[7] = (ds >> 24) & 0xFF
    for i, p in enumerate(_loc_records(data)):   # re-parse post-splice for fresh offsets
        if p["midx_off"] is not None:
            o, s = p["midx_off"], p["midx_size"]
            data[o:o + s] = i.to_bytes(s, "little")
    _fix_crcs(data)
    return data


# ---- watch I/O (the swap: pull live, verify, delete+upload, sync backup) -------

def _mtp(*args, timeout=300):
    return subprocess.run(["bash", MTP, *args], capture_output=True, text=True, timeout=timeout)


def _pull_live(dest_dir):
    """Pull the watch's current Lctns.fit into dest_dir. Returns its path or None."""
    p = _mtp("get-all", WATCH_LEAF, dest_dir)
    sys.stderr.write(p.stderr)
    for f in os.listdir(dest_dir):
        if f.lower() == WATCH_FNAME.lower():
            return os.path.join(dest_dir, f)
    return None


def _watch_index():
    """The MTP index of Lctns.fit in GARMIN/Locations (for delete-before-upload), or None."""
    p = _mtp("list", WATCH_LEAF)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        return None
    for line in p.stdout.splitlines():
        if "\t" in line:
            idx, fname = line.split("\t", 1)
            if idx.strip().isdigit() and fname.strip().lower() == WATCH_FNAME.lower():
                return int(idx)
    return None


def _push_live(local_path):
    """Replace the watch's Lctns.fit: delete the current one (if present) then upload the new
    file (uploaded under its local basename, which MUST be Lctns.fit). Returns True on upload
    success."""
    idx = _watch_index()
    if idx is not None:
        d = _mtp("delete", str(idx), WATCH_LEAF)
        sys.stderr.write(d.stderr)
    p = _mtp("upload", local_path, WATCH_LEAF)
    sys.stderr.write(p.stderr)
    return p.returncode == 0


def apply_edit(op, index, new_name=None, expect_name=None):
    """Perform a rename/delete against the LIVE watch file. Steps: pull live -> parse ->
    optimistic-concurrency guard (target index still present and, if expect_name given, its
    name unchanged) -> edit -> decode-verify -> delete+upload -> sync the Mac backup. Returns
    a result dict; never writes the watch unless every gate passed."""
    work = tempfile.mkdtemp(prefix="gb_loc_")
    try:
        live = _pull_live(work)
        if not live:
            return {"ok": False, "reason": "no_live_file",
                    "message": "Could not read the saved-points file from your Fenix."}
        data = bytearray(open(live, "rb").read())
        if not _crcs_ok(data):
            return {"ok": False, "reason": "live_corrupt",
                    "message": "The saved-points file on your Fenix did not verify; nothing changed."}
        cur = next((p for p in _loc_records(data) if p["index"] == index), None)
        if not cur:
            return {"ok": False, "reason": "index_gone",
                    "message": "That saved point is no longer on your Fenix; refresh and try again."}
        if expect_name is not None and cur["name"] != expect_name:
            return {"ok": False, "reason": "drifted",
                    "message": f"That saved point changed on your Fenix (now {cur['name']!r}); "
                               "refresh and try again."}
        before = len(_loc_records(data))
        if op == "rename":
            try:
                new = rename_bytes(data, index, new_name)
            except ValueError as e:
                reason = "name_too_long" if "name_too_long" in str(e) else "edit_failed"
                return {"ok": False, "reason": reason, "message": str(e)}
            expect_count, old_name = before, cur["name"]
        elif op == "delete":
            new = delete_bytes(data, index)
            expect_count, old_name = before - 1, cur["name"]
        else:
            return {"ok": False, "reason": "bad_op", "message": f"unknown op {op}"}

        try:
            pts = verify(new, expect_count)
        except ValueError as e:
            return {"ok": False, "reason": "verify_failed",
                    "message": f"The edited file failed verification ({e}); your Fenix was not touched."}

        out = os.path.join(work, WATCH_FNAME)
        with open(out, "wb") as f:
            f.write(new)
        if not _push_live(out):
            return {"ok": False, "reason": "watch_write_failed",
                    "message": "Writing to your Fenix failed; the saved points were not changed."}
        # Sync the Mac backup to the file we just put on the watch (self-heals any prior drift).
        backup_path = mac_backup_path()
        os.makedirs(os.path.dirname(backup_path), exist_ok=True)
        shutil.copyfile(out, backup_path)
        return {"ok": True, "op": op, "index": index, "old_name": old_name,
                "new_name": new_name, "points": pts}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---- preview (offline, from the Mac backup) -----------------------------------

def preview(op, index, new_name=None):
    pts = points_from_backup()
    p = next((p for p in pts if p["index"] == index), None)
    if not p:
        return {"ok": False, "reason": "not_found", "message": f"no saved point with index {index}"}
    if op == "rename":
        data = open(mac_backup_path(), "rb").read()
        slot = name_slot(bytearray(data)) or 0
        nn = (new_name or "").strip()
        fits = len(nn.encode("utf-8")) <= slot
        return {"ok": True, "op": "rename", "index": index, "old_name": p["name"],
                "new_name": nn, "name_max": slot, "fits": fits}
    if op == "delete":
        return {"ok": True, "op": "delete", "index": index, "name": p["name"],
                "remaining": len(pts) - 1}
    return {"ok": False, "reason": "bad_op", "message": f"unknown op {op}"}


# ---- cli ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="GarminBridge saved-points manager")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    for name in ("rename", "delete"):
        s = sub.add_parser(name)
        s.add_argument("--index", type=int, required=True)
        s.add_argument("--expect", help="the point's current name (concurrency guard)")
        s.add_argument("--yes", action="store_true")
        if name == "rename":
            s.add_argument("--to", dest="to", required=True)
    args = ap.parse_args()

    if args.cmd == "list":
        print(json.dumps({"ok": True, "points": points_from_backup()}, ensure_ascii=False))
        return

    op = args.cmd
    if not args.yes:  # dry-run: offline plan from the Mac backup
        plan = preview(op, args.index, getattr(args, "to", None))
        print(json.dumps(plan, ensure_ascii=False))
        return

    res = apply_edit(op, args.index, new_name=getattr(args, "to", None), expect_name=args.expect)
    print(json.dumps(res, ensure_ascii=False))
    sys.exit(0 if res.get("ok") else 1)


if __name__ == "__main__":
    main()
