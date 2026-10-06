#!/usr/bin/env python3
"""content.py — GarminBridge unified content manager.

Cross-refs Connect (workouts + courses) against the watch (GARMIN/Workouts +
GARMIN/Courses over USB) into a placement matrix, and exposes the proven primitives
as guarded actions. The awareness layer the whole platform builds on.

States per item ([Connect][Watch]):
  ● ●  synced        · ● ○  connect-only (decoupled / never sent)
  ○ ●  watch-only (stranded / old version / direct add)  · scheduled (plan re-sync)

Commands:
  status         (default) the placement matrix
  add-to-watch   push Connect item(s) onto the watch  (download FIT -> USB upload)
  rm-watch       remove watch file(s) over USB
  rm-connect     remove Connect item(s) via the API   (by explicit id only)
  clean-watch    bulk-remove the watch-only items of a kind

Select with --kind workout|course and one of:
  --name <substr>   name contains <substr> (case-insensitive)
  --id <id>         a Connect item by id (add-to-watch / rm-connect)
  --state <s>       a whole bucket: synced|connect-only|watch-only|scheduled
Destructive actions are DRY-RUN unless --yes. clean-watch/rm-watch never touch synced
or scheduled items unless named explicitly; rm-connect deletes ONLY by explicit id
(see memory delete-only-by-explicit-id).

COURSES — robust matching: the watch stores course files under Garmin's lossy ~30-char
names (truncation, _N dup suffix, .fit.fit), so a filename match can mislabel a current
route as watch-only (it once false-deleted 6 live routes). Fix: build_watch_manifest.py
reads each FIT's INTERNAL course.name into watch_manifest.json; matching uses that when
present. clean-watch --kind course REFUSES to delete any file not internal-name-verified.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

import inventory
import fit_name_patch
from clean_watch import SCHED_RE, _norm, _stem
from garminconnect import Garmin
import paths

HERE = os.path.dirname(os.path.abspath(__file__))
MTP = paths.asset("watch_mtp.sh")
WATCH_FOLDER = {"workout": "GARMIN/Workouts", "course": "GARMIN/Courses"}
# Connect has NO course FIT export (every /download-service + /course-service export path
# 404s, probed 2026-06-30), so course add-to-watch sources the FIT from the Mac backup —
# the same dir the 6 false-deleted routes were restored from.


def course_backup_dir():
    """Mac course FIT library (under GVE_SET_DEST)."""
    return os.path.join(paths.bridge_settings_dir(), "Courses")


# ---- matching + watch I/O -----------------------------------------------------

def keys_match(a, b):
    """Conservative (biased to KEEP): equality or prefix-containment either way."""
    return bool(a) and bool(b) and (a == b or a.startswith(b) or b.startswith(a))


def watch_list(leaf):
    p = subprocess.run(["bash", MTP, "list", leaf], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise SystemExit(f"watch list failed for {leaf} (is the watch on USB?) rc={p.returncode}")
    out = []
    for line in p.stdout.splitlines():
        if "\t" in line:
            idx, fname = line.split("\t", 1)
            if idx.strip().isdigit() and fname.strip():
                out.append((int(idx), fname.strip()))
    return out


def watch_scan(leaves):
    """FAST snapshot read: list ALL given folder leaves in ONE MTP session (watch_mtp.sh
    scan) instead of one `list` call per folder. Same per-file model as watch_list; returns
    {leaf: [(idx, fname), ...]}."""
    p = subprocess.run(["bash", MTP, "scan", ",".join(leaves)], capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise SystemExit(f"watch scan failed (is the watch on USB?) rc={p.returncode}")
    out = {leaf: [] for leaf in leaves}
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[1].strip().isdigit() and parts[0] in out and parts[2].strip():
            out[parts[0]].append((int(parts[1]), parts[2].strip()))
    return out


def load_manifest():
    """filename -> internal name, per kind; cached by build_watch_manifest.py while the
    watch is connected. Lets matching use the FIT's real (clean) name instead of the
    lossy filename, and works OFFLINE once cached."""
    p = paths.state("watch_manifest.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def _manifest_has(fname, kind, manifest):
    """True iff this watch file's INTERNAL name was read from its FIT (cached in the
    manifest). Only then is a watch-only verdict trustworthy enough to delete on."""
    return bool(fname and (manifest.get(kind) or {}).get(fname))


def _watch_key(fname, kind, manifest):
    nm = (manifest.get(kind) or {}).get(fname)
    return _norm(nm) if nm else _norm(_stem(fname))


def _watch_display_name(fname, kind, manifest):
    return (manifest.get(kind) or {}).get(fname) or fname


def backup_course_fit(target_name):
    """Find the Mac-backup course FIT whose INTERNAL name matches target_name. Matches on
    the FIT's real course.name (never the lossy backup filename — same lesson as the watch
    manifest). Returns (path, internal_name) or (None, None)."""
    cbd = course_backup_dir()
    if not os.path.isdir(cbd):
        return None, None
    from build_watch_manifest import internal_name  # lazy: only needs fitdecode here
    tkey = _norm(target_name)
    for f in sorted(os.listdir(cbd)):
        if not f.lower().endswith(".fit"):
            continue
        path = os.path.join(cbd, f)
        nm = internal_name(path, "course", "name")
        if nm and keys_match(tkey, _norm(nm)):
            return path, nm
    return None, None


def build(connect_items, watch_items, kind, manifest=None):
    """Cross-ref one content type into placement rows (with the watch file index).
    Each row carries `manifest_verified` = was the watch file matched on its internal
    FIT name (reliable) vs Garmin's lossy filename (can false-flag a current item)."""
    manifest = manifest or {}
    citems = [c for c in connect_items if c["kind"] == kind]
    wfiles = [(i, f, _watch_key(f, kind, manifest)) for i, f in watch_items]
    claimed, rows = set(), []
    for c in citems:
        ckey = _norm(c["name"])
        m = next(((i, f) for i, f, wk in wfiles if i not in claimed and keys_match(ckey, wk)), None)
        if m:
            claimed.add(m[0])
        rows.append({"kind": kind, "name": c["name"], "id": c["id"], "in_connect": True,
                     "on_watch": bool(m), "watch_file": m[1] if m else None,
                     "watch_idx": m[0] if m else None, "scheduled": False,
                     "manifest_verified": _manifest_has(m[1], kind, manifest) if m else False})
    for i, f, wk in wfiles:
        if i not in claimed:
            rows.append({"kind": kind, "name": _watch_display_name(f, kind, manifest),
                         "id": None, "in_connect": False,
                         "on_watch": True, "watch_file": f, "watch_idx": i,
                         "scheduled": bool(SCHED_RE.match(f)),
                         "manifest_verified": _manifest_has(f, kind, manifest)})
    return rows


def state(r):
    if r["scheduled"]:
        return "scheduled"
    if r["in_connect"]:
        return "synced" if r["on_watch"] else "connect-only"
    return "watch-only"


def login():
    g = Garmin()
    g.login(paths.state(".tokens"))
    return g


def connect_items(g):
    return inventory.connect_workouts(g) + inventory.connect_courses(g)


def matrix(g, kind):
    return build(connect_items(g), watch_list(WATCH_FOLDER[kind]), kind, load_manifest())


def select(rows, args):
    """Filter rows by --name / --id / --state / --watch-file."""
    out = rows
    if args.state:
        out = [r for r in out if state(r) == args.state]
    if args.name:
        q = args.name.lower()
        out = [r for r in out if q in (r["name"] or "").lower()]
    if args.id:
        out = [r for r in out if str(r["id"]) == str(args.id)]
    # --watch-file: EXACT on-watch filename(s), comma-separated. The UI uses this to
    # target one (or several precisely chosen) watch files without the fuzzy substring
    # match — so a row's badge tap removes exactly that file, never an accidental sibling.
    if getattr(args, "watch_file", None):
        wanted = {w for w in args.watch_file.split(",") if w}
        out = [r for r in out if r.get("watch_file") in wanted]
    return out


# ---- views --------------------------------------------------------------------

def cmd_status(g, args):
    kinds = [args.kind] if args.kind else ["workout", "course"]
    for kind in kinds:
        rows = matrix(g, kind)
        b = {k: [] for k in ("synced", "connect-only", "watch-only", "scheduled")}
        for r in rows:
            b[state(r)].append(r)
        print(f"\n=== {kind.upper()}S | {len(b['synced'])} synced · "
              f"{len(b['connect-only'])} connect-only · {len(b['watch-only'])} watch-only · "
              f"{len(b['scheduled'])} scheduled ===")
        for key, label in (("watch-only", "ON WATCH, NOT IN CONNECT (stranded / old version / direct-add)"),
                           ("connect-only", "IN CONNECT, NOT ON WATCH (decoupled / never sent)"),
                           ("synced", "SYNCED (both)"),
                           ("scheduled", "SCHEDULED (plan re-syncs; informational)")):
            rs = b[key]
            if not rs:
                continue
            if key in ("synced", "connect-only") and not args.all and len(rs) > 12:
                print(f"\n  {label}: {len(rs)} (use --all to list)")
                continue
            print(f"\n  {label}:")
            for r in sorted(rs, key=lambda x: (x["name"] or "").lower()):
                idp = f" id={r['id']}" if r["id"] else ""
                print(f"    [C {'●' if r['in_connect'] else '○'}]"
                      f"[W {'●' if r['on_watch'] else '○'}]  {(r['name'] or '')[:46]:46}{idp}")


# ---- actions ------------------------------------------------------------------

def _watch_delete(idxs, leaf):
    csv = ",".join(str(i) for i in idxs)
    p = subprocess.run(["bash", MTP, "delete", csv, leaf], capture_output=True, text=True)
    sys.stderr.write(p.stderr)
    return p.returncode == 0


def cmd_rm_watch(g, args, _rows=None):
    if not args.kind:
        raise SystemExit("rm-watch needs --kind")
    rows = _rows if _rows is not None else select(matrix(g, args.kind), args)
    targets = [r for r in rows if r.get("watch_idx") is not None]
    # guard: never silently remove a SYNCED or SCHEDULED file unless explicitly named
    risky = [r for r in targets if state(r) in ("synced", "scheduled")]
    if risky and not (args.name or args.id or getattr(args, "watch_file", None)):
        print("Refusing: selection includes synced/scheduled files. Narrow with --name, "
              "or use --state watch-only.")
        for r in risky:
            print(f"  ! {state(r):12} {r['watch_file']}")
        return
    if not targets:
        print("Nothing to remove.")
        return
    print(f"Would remove {len(targets)} watch file(s) from {WATCH_FOLDER[args.kind]}:")
    for r in sorted(targets, key=lambda x: x["watch_idx"]):
        print(f"  #{r['watch_idx']:<4} [{state(r)}] {r['watch_file']}")
    if not args.yes:
        print("\nDRY-RUN. Re-run with --yes to delete.")
        return
    ok = _watch_delete([r["watch_idx"] for r in targets], WATCH_FOLDER[args.kind])
    print("done." if ok else "delete reported an error — re-run status to check.")


def cmd_clean_watch(g, args):
    if not args.kind:
        raise SystemExit("clean-watch needs --kind")
    rows = [r for r in matrix(g, args.kind) if state(r) == "watch-only"]
    if args.kind == "course":
        # SAFETY GUARD (memory delete-only-by-explicit-id): a watch-only course is only
        # safe to delete if its INTERNAL FIT name was read into the manifest. Garmin's
        # on-watch course filenames are lossy (truncation/_N/.fit.fit) and a filename-only
        # match once false-flagged 6 CURRENT routes as orphaned. So REFUSE any course that
        # isn't internal-name-verified and clean only the verified orphans.
        verified = [r for r in rows if r.get("manifest_verified")]
        unverified = [r for r in rows if not r.get("manifest_verified")]
        if unverified:
            print(f"REFUSING {len(unverified)} course file(s): not internal-name-verified — a "
                  "filename-only match can mislabel a CURRENT route as orphaned.")
            print("Connect the watch and build the manifest first, then re-run:")
            print("  ./watch_mtp.sh get-all GARMIN/Courses /tmp/courses")
            print("  ./build_watch_manifest.py course /tmp/courses")
            for r in sorted(unverified, key=lambda x: x["watch_file"] or ""):
                print(f"  ?  {r['watch_file']}")
            print()
        if not verified:
            print("No internal-name-verified orphans to clean. Nothing to do.")
            return
        print(f"Internal-name-verified orphans (read from FIT, safe to delete): {len(verified)}\n")
        rows = verified
    args.state = None  # already filtered to watch-only
    cmd_rm_watch(g, args, _rows=rows)


def _select_connect(items, args):
    if args.id:
        return [c for c in items if str(c["id"]) == str(args.id)]
    if args.name:
        q = args.name.lower()
        return [c for c in items if q in (c["name"] or "").lower()]
    raise SystemExit("add-to-watch needs --name or --id")


def cmd_add_to_watch(g, args):
    if args.kind == "workout":
        items = _select_connect(inventory.connect_workouts(g), args)
        if not items:
            raise SystemExit("no matching Connect workout.")
        print(f"Would push {len(items)} workout(s) to the watch:")
        for c in items:
            print(f"  id={c['id']}  {c['name']!r}")
        if not args.yes:
            print("\nDRY-RUN. Re-run with --yes to push.")
            return
        for c in items:
            fit = g.download_workout(c["id"])
            safe = re.sub(r"[^A-Za-z0-9._-]", "_", c["name"])[:40]
            path = os.path.join(tempfile.gettempdir(), f"{safe}_workout.fit")
            with open(path, "wb") as f:
                f.write(fit)
            p = subprocess.run(["bash", MTP, "upload", path, WATCH_FOLDER["workout"]],
                               capture_output=True, text=True)
            sys.stderr.write(p.stderr)
            print(f"  pushed {c['name']!r}" if p.returncode == 0 else f"  FAILED {c['name']!r}")
        return

    if args.kind == "course":
        items = _select_connect(inventory.connect_courses(g), args)
        if not items:
            raise SystemExit("no matching Connect course.")
        resolved = [(c, *backup_course_fit(c["name"])) for c in items]
        print(f"Would push {len(resolved)} course(s) to the watch (source: Mac backup):")
        for c, path, nm in resolved:
            print(f"  id={c['id']}  {c['name']!r}  <- "
                  f"{os.path.basename(path) if path else 'NO BACKUP FIT FOUND'}")
        missing = [c for c, p, _ in resolved if not p]
        if missing:
            print(f"\n{len(missing)} course(s) have no matching backup FIT. Connect has no FIT "
                  "export (endpoints 404), so a course not in the Mac backup can't be pushed yet.")
        if not args.yes:
            print("\nDRY-RUN. Re-run with --yes to push.")
            return
        for c, path, nm in resolved:
            if not path:
                print(f"  SKIP {c['name']!r} (no backup FIT)")
                continue
            p = subprocess.run(["bash", MTP, "upload", path, WATCH_FOLDER["course"]],
                               capture_output=True, text=True)
            sys.stderr.write(p.stderr)
            print(f"  pushed {c['name']!r}" if p.returncode == 0 else f"  FAILED {c['name']!r}")
        return

    raise SystemExit("add-to-watch needs --kind workout|course")


def cmd_rm_connect(g, args):
    if not args.id:
        raise SystemExit("rm-connect deletes ONLY by explicit --id (safety guard).")
    if args.kind == "course":
        item = next((c for c in inventory.connect_courses(g) if str(c["id"]) == str(args.id)), None)
        if not item:
            raise SystemExit(f"no Connect course with id={args.id}")
        print(f"Would delete from Connect: COURSE id={item['id']} {item['name']!r}")
        print("WARNING: a course delete is PERMANENT (no Connect trash) and does NOT remove the "
              "on-watch copy — use rm-watch/clean-watch for the device.")
        if not args.yes:
            print("\nDRY-RUN. Re-run with --yes to delete.")
            return
        # DELETE /course-service/course/{id} — method confirmed via OPTIONS (Allow: ...,DELETE,...)
        g.client.request("DELETE", "connectapi", f"/course-service/course/{item['id']}", api=True)
        print("deleted from Connect.")
        return
    item = next((c for c in inventory.connect_workouts(g) if str(c["id"]) == str(args.id)), None)
    if not item:
        raise SystemExit(f"no Connect workout with id={args.id}")
    print(f"Would delete from Connect: id={item['id']} {item['name']!r}")
    if not args.yes:
        print("\nDRY-RUN. Re-run with --yes to delete.")
        return
    g.delete_workout(item["id"])
    print("deleted from Connect.")


# ---- rename (propagates: Connect + watch) -------------------------------------

def _svc_field(kind):
    svc = "workout-service/workout" if kind == "workout" else "course-service/course"
    field = "workoutName" if kind == "workout" else "courseName"
    return svc, field


def connect_name(g, kind, cid):
    """The item's current name via a SINGLE Connect GET (no inventory fetch). Lets the app
    resolve one row's name without pulling every workout+course. Returns None if not found."""
    svc, field = _svc_field(kind)
    try:
        payload = g.client.request("GET", "connectapi", f"/{svc}/{cid}", api=True).json()
    except Exception:
        return None
    return payload.get(field)


def _connect_rename(g, kind, cid, new_name):
    """Rename in Connect via raw GET -> change the name field -> PUT (garminconnect has no
    update method). Course edits are rate-limited (429 'not yet ready to update') so we
    surface that instead of crashing. Returns (ok, old_name, msg)."""
    svc, field = _svc_field(kind)
    path = f"/{svc}/{cid}"
    payload = g.client.request("GET", "connectapi", path, api=True).json()
    old = payload.get(field)
    if old == new_name:
        return True, old, "unchanged"
    payload[field] = new_name
    try:
        r = g.client.request("PUT", "connectapi", path, api=True, json=payload)
    except Exception as e:
        m = str(e)
        if "429" in m:
            return False, old, "Garmin is rate-limiting edits to this item; try again in a moment"
        return False, old, f"Connect rename failed: {m[:100]}"
    return getattr(r, "status_code", 200) in (200, 204), old, "ok"


def _course_gps(path):
    """(internal course.name, GPS-point count) via fitdecode — for the post-patch gate."""
    import fitdecode
    nm, gps = None, 0
    with fitdecode.FitReader(path) as fr:
        for f in fr:
            if isinstance(f, fitdecode.FitDataMessage):
                if f.name == "course" and f.has_field("name"):
                    nm = f.get_value("name")
                if f.name == "record" and f.has_field("position_lat") \
                        and f.get_value("position_lat") is not None:
                    gps += 1
    return nm, gps


def _watch_swap_workout(g, cid, new_name, watch_idx):
    """Refresh the on-watch workout with its new Connect name: re-download (the fresh FIT
    embeds the new wkt_name) then delete the old file and upload the new one."""
    fit = g.download_workout(cid)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", new_name)[:40] or "workout"
    path = os.path.join(tempfile.gettempdir(), f"{safe}_workout.fit")
    with open(path, "wb") as f:
        f.write(fit)
    if watch_idx is not None:
        _watch_delete([watch_idx], WATCH_FOLDER["workout"])
    p = subprocess.run(["bash", MTP, "upload", path, WATCH_FOLDER["workout"]],
                       capture_output=True, text=True)
    sys.stderr.write(p.stderr)
    print("  watch: updated" if p.returncode == 0
          else "  watch: upload FAILED (item is in Connect; re-add from the app)")


def _watch_swap_course(old_name, new_name, watch_idx):
    """Rename the on-watch course losslessly: patch a copy of the Mac-backup FIT's internal
    name (never a re-encode), GATE on a decode-verify (new name + identical GPS track), then
    delete the old watch file, upload the patched one, and sync the Mac backup to match."""
    import shutil
    src, _ = backup_course_fit(old_name)
    if not src:
        print("  watch: no Mac-backup FIT for this route; on-watch copy keeps the old name")
        return
    tmpdir = tempfile.mkdtemp(prefix="gb_rename_")
    tmp = os.path.join(tmpdir, os.path.basename(src))  # keep a sensible on-watch filename
    res = fit_name_patch.patch_name(src, new_name, dst=tmp)
    if not res["ok"]:
        why = "name longer than the on-watch file allows" if res["reason"] == "name_too_long" \
            else res["reason"]
        print(f"  watch: not renamed ({why}); on-watch copy keeps the old name")
        return
    nm, gps = _course_gps(tmp)
    _, gps0 = _course_gps(src)
    if not (nm == new_name and gps == gps0 and gps0 > 0):
        print("  watch: patched FIT failed verification; on-watch copy left untouched")
        return
    if watch_idx is not None:
        _watch_delete([watch_idx], WATCH_FOLDER["course"])
    p = subprocess.run(["bash", MTP, "upload", tmp, WATCH_FOLDER["course"]],
                       capture_output=True, text=True)
    sys.stderr.write(p.stderr)
    if p.returncode == 0:
        shutil.copyfile(tmp, src)  # keep the backup (re-add source of truth) in sync
        print("  watch: updated (Mac backup renamed to match)")
    else:
        print("  watch: upload FAILED; on-watch copy removed but re-addable from the app")


def course_watch_name_max(connect_name):
    """The real byte ceiling for renaming a course ON THE WATCH: the allocated size of the
    on-watch (Mac-backup) FIT's name field. Garmin often TRUNCATES the course name in the
    FIT (e.g. a 43-char Connect name stored as 30 bytes on the watch), so this can be far
    shorter than the Connect name — it, not the Connect name length, is the true limit.
    Returns the byte size, or None if there's no backup FIT / no name field."""
    src, _ = backup_course_fit(connect_name)
    if not src:
        return None
    data = bytearray(open(src, "rb").read())
    hits, _, _ = fit_name_patch.find_name_field(data, 31, 5)
    return hits[0][1] if hits else None


def _rename_placement(g, args):
    """Where the item lives, doing the LEAST work. The app already knows this from its
    snapshot, so when it passes --on-watch/--watch-idx we TRUST it: no inventory fetch and no
    USB watch scan — the two things that made rename slow, and that made a Connect-only rename
    FAIL whenever the Fenix was unplugged. Standalone CLI still derives it from the matrix, but
    an absent watch counts as 'not on the watch': a Connect rename must never fail just because
    the watch isn't plugged in. Returns (on_watch, watch_idx)."""
    scheduled = getattr(args, "scheduled", False)
    ow = getattr(args, "on_watch", None)
    if ow is not None:
        wi = getattr(args, "watch_idx", None)
        return (ow == "1" and not scheduled), (int(wi) if wi not in (None, "") else None)
    c = next((x for x in connect_items(g)
              if x["kind"] == args.kind and str(x["id"]) == str(args.id)), None)
    if not c:
        raise SystemExit(f"no Connect {args.kind} with id={args.id}")
    try:
        witems = watch_list(WATCH_FOLDER[args.kind])
    except SystemExit:
        witems = []  # watch not on USB -> rename in Connect only, don't fail
    row = next((r for r in build([c], witems, args.kind, load_manifest())
                if str(r["id"]) == str(args.id)), None)
    return (bool(row and row["on_watch"] and not row["scheduled"]),
            row.get("watch_idx") if row else None)


def cmd_rename(g, args):
    if not args.kind or not args.id or not getattr(args, "to", None):
        raise SystemExit("rename needs --kind, --id and --to \"New Name\"")
    new_name = args.to.strip()
    if not new_name:
        raise SystemExit("rename --to must be a non-empty name")
    on_watch, watch_idx = _rename_placement(g, args)
    print(f"Rename {args.kind} id={args.id} -> {new_name!r}"
          + ("  (updates the on-watch copy too)" if on_watch else "  (Connect only)"))
    if not args.yes:
        print("\nDRY-RUN. Re-run with --yes to rename.")
        return

    ok, old_name, msg = _connect_rename(g, args.kind, args.id, new_name)
    print(f"  Connect rename: {'done' if ok else 'FAILED'} ({msg})")
    if not ok:
        raise SystemExit(msg if msg and msg != "ok" else "Connect rename failed; the watch copy was left unchanged.")
    if on_watch:
        if args.kind == "workout":
            _watch_swap_workout(g, args.id, new_name, watch_idx)
        else:
            _watch_swap_course(old_name, new_name, watch_idx)
    print("rename complete.")


# ---- cli ----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="GarminBridge unified content manager")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("status", "add-to-watch", "rm-watch", "rm-connect", "clean-watch", "rename"):
        s = sub.add_parser(name)
        s.add_argument("--kind", choices=["workout", "course"])
        s.add_argument("--name")
        s.add_argument("--id")
        s.add_argument("--to", dest="to", help="new name (rename)")
        s.add_argument("--watch-file", dest="watch_file",
                       help="exact on-watch filename(s), comma-separated (precise rm-watch)")
        s.add_argument("--state", choices=["synced", "connect-only", "watch-only", "scheduled"])
        # Caller-known placement (app fast path): skips the inventory fetch + USB watch scan.
        s.add_argument("--on-watch", dest="on_watch", choices=["0", "1"])
        s.add_argument("--watch-idx", dest="watch_idx")
        s.add_argument("--scheduled", action="store_true")
        s.add_argument("--yes", action="store_true")
        s.add_argument("--all", action="store_true")
    args = ap.parse_args()
    cmd = args.cmd or "status"

    g = login()
    {"status": cmd_status, "add-to-watch": cmd_add_to_watch, "rm-watch": cmd_rm_watch,
     "rm-connect": cmd_rm_connect, "clean-watch": cmd_clean_watch,
     "rename": cmd_rename}[cmd](g, args)


if __name__ == "__main__":
    main()
