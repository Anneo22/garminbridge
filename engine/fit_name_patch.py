#!/usr/bin/env python3
"""fit_name_patch.py — losslessly rename a FIT file's INTERNAL name.

Why not just re-encode with fit_tool? Because it silently DROPS Garmin-proprietary
fields it doesn't know (record 121/122, lap 27-30, course 254 — verified 2026-07-01),
which could strand a broken route on the watch. So instead we patch ONLY the name
field's bytes in place and recompute the trailing FIT CRC — every GPS point, turn, and
proprietary byte stays identical. The one constraint: the new name must fit in the byte
size the file already allocated for the name (Garmin pads it), else we refuse (the caller
falls back to a Connect-only rename rather than risk a structural rewrite).

Public API:
  patch_name(src, new_name, dst=None, global_msg=31, field_def=5) -> dict
    global_msg/field_def default to course.name; workouts use 26/8 (workout.wkt_name).
    Returns {ok, reason, old_name, new_name, allocated, needed}. Writes dst (default: src).
"""
import os
import sys

# FIT CRC-16 (nibble table form, per the FIT SDK).
_CRC = [0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
        0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400]


def fit_crc(data):
    crc = 0
    for b in data:
        t = _CRC[crc & 0xF]; crc = (crc >> 4) & 0x0FFF; crc = crc ^ t ^ _CRC[b & 0xF]
        t = _CRC[crc & 0xF]; crc = (crc >> 4) & 0x0FFF; crc = crc ^ t ^ _CRC[(b >> 4) & 0xF]
    return crc & 0xFFFF


def _u16(b, i):
    return b[i] | (b[i + 1] << 8)


def find_name_field(data, global_msg, field_def):
    """Walk the FIT records and return every (offset, size) where the given message's
    string field lives, plus (header_size, data_end). Pure structural parse — tracks the
    active definition per local message type; never interprets field values."""
    hdr = data[0]
    if hdr not in (12, 14):
        raise ValueError(f"unexpected FIT header size {hdr}")
    data_end = len(data) - 2  # last 2 bytes are the file CRC
    defs = {}  # local type -> (global_msg_num, [(field_def_num, size)], has_dev, [dev sizes])
    pos = hdr
    hits = []
    while pos < data_end:
        rec = data[pos]
        if rec & 0x80:
            raise ValueError("compressed-timestamp header not supported for in-place patch")
        local = rec & 0x0F
        if rec & 0x40:  # definition message
            base = pos + 1
            reserved = data[base]           # noqa: F841
            arch = data[base + 1]           # 0 = little-endian, 1 = big-endian
            # global_msg_num is the ONLY endian-sensitive part we read; field def nums,
            # sizes and the name string are all single bytes / byte arrays. Some Garmin
            # course files are big-endian (verified 2026-07-01) — read it accordingly or
            # msg 31 (course) is misread as 7936 and the name is silently not found.
            gmsg = ((data[base + 2] << 8) | data[base + 3]) if arch else _u16(data, base + 2)
            nfields = data[base + 4]
            p = base + 5
            fields = []
            for _ in range(nfields):
                fields.append((data[p], data[p + 1]))  # (field_def_num, size)
                p += 3
            has_dev = bool(rec & 0x20)
            devsz = []
            if has_dev:
                ndev = data[p]; p += 1
                for _ in range(ndev):
                    devsz.append(data[p + 1])
                    p += 3
            defs[local] = (gmsg, fields, has_dev, devsz)
            pos = p
        else:  # data message
            gmsg, fields, has_dev, devsz = defs[local]
            off = pos + 1
            for fnum, fsize in fields:
                if gmsg == global_msg and fnum == field_def:
                    hits.append((off, fsize))
                off += fsize
            for dsz in devsz:
                off += dsz
            pos = off
    return hits, hdr, data_end


def read_name(data, offset, size):
    raw = bytes(data[offset:offset + size])
    return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")


def name_fits(src, new_name, global_msg=31, field_def=5):
    """Can new_name be patched into src in place? True/False, or None if the name field
    isn't found. (Equivalent client-side rule: len(new utf-8) <= len(current utf-8), since
    Garmin allocates the field to the current name's exact byte length.)"""
    data = bytearray(open(src, "rb").read())
    hits, _, _ = find_name_field(data, global_msg, field_def)
    if not hits:
        return None
    return len(new_name.encode("utf-8")) <= hits[0][1]


def crc_range_ok(data):
    """Verify our CRC function + range reproduce the file's stored CRC. Proves we can
    safely rewrite it. FIT's file CRC covers all bytes from 0 to the CRC itself."""
    stored = _u16(data, len(data) - 2)
    return fit_crc(data[: len(data) - 2]) == stored


def patch_name(src, new_name, dst=None, global_msg=31, field_def=5):
    dst = dst or src
    data = bytearray(open(src, "rb").read())
    if not crc_range_ok(data):
        return {"ok": False, "reason": "crc_selfcheck_failed", "old_name": None,
                "new_name": new_name}
    hits, hdr, data_end = find_name_field(data, global_msg, field_def)
    if not hits:
        return {"ok": False, "reason": "name_field_not_found", "old_name": None,
                "new_name": new_name}
    off, size = hits[0]
    old_name = read_name(data, off, size)
    enc = new_name.encode("utf-8")
    # Garmin allocates the name field to the exact byte length of the original name (no
    # spare, often no null terminator). So the new name fits in place iff its UTF-8 length
    # is <= that allocation; a shorter name is null-padded, an equal-length one fills the
    # field exactly (the same convention Garmin itself uses). Longer names can't be patched
    # in place without a structural rewrite, so we refuse and the caller renames on Connect
    # only. In-place keeps every other byte identical — the whole point.
    if len(enc) > size:
        return {"ok": False, "reason": "name_too_long", "old_name": old_name,
                "new_name": new_name, "allocated": size, "needed": len(enc)}
    for off, size in hits:  # patch every matching message (usually one)
        data[off:off + size] = enc + b"\x00" * (size - len(enc))
    crc = fit_crc(data[: data_end])
    data[data_end] = crc & 0xFF
    data[data_end + 1] = (crc >> 8) & 0xFF
    with open(dst, "wb") as f:
        f.write(data)
    return {"ok": True, "reason": "patched", "old_name": old_name, "new_name": new_name,
            "allocated": size}


if __name__ == "__main__":
    # smoke: patch_name <src> <new_name> [dst]
    r = patch_name(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
    print(r)
    sys.exit(0 if r["ok"] else 1)
