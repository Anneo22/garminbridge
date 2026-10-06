#!/usr/bin/env python3
import os
import shutil
import tempfile

import api


def write_file(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    kwargs = {} if mode == "wb" else {"encoding": "utf-8"}
    with open(path, mode, **kwargs) as f:
        f.write(data)


def assert_exists(path):
    assert os.path.exists(path), f"expected file to exist: {path}"


def main():
    root = tempfile.mkdtemp()
    try:
        archive = os.path.join(root, "Archive")
        old_archived = os.path.join(archive, "20250101_000000.wav")
        old_txt = os.path.join(archive, "20250101_000000.txt")
        old_raw = os.path.join(archive, "20250101_000000.raw.txt")
        recent_archived = os.path.join(archive, "20260704_120000.wav")
        active_old = os.path.join(root, "20250101_000010.wav")
        note = os.path.join(root, "note.md")

        write_file(old_archived, b"old audio")
        write_file(old_txt, "transcript")
        write_file(old_raw, "raw transcript")
        write_file(recent_archived, b"recent audio")
        write_file(active_old, b"active audio")
        write_file(note, "# note\n")

        candidates = api.archived_cleanup_candidates(archive, 90)
        assert [c["path"] for c in candidates] == [os.path.realpath(old_archived)], candidates
        assert candidates[0]["bytes"] == os.path.getsize(old_archived), candidates

        old_size = os.path.getsize(old_archived)
        res = api.delete_archived_wavs(candidates, archive, 90)
        assert res["deleted"] == [os.path.realpath(old_archived)], res
        assert res["freed_bytes"] == old_size, res
        assert res["skipped"] == [], res

        assert not os.path.exists(old_archived), "old archived wav should be deleted"
        assert_exists(recent_archived)
        assert_exists(old_txt)
        assert_exists(old_raw)
        assert_exists(active_old)
        assert_exists(note)

        outside = api.delete_archived_wavs([{"path": active_old}], archive, 90)
        assert outside["deleted"] == [], outside
        assert outside["skipped"] == [os.path.realpath(active_old)], outside
        assert_exists(active_old)

        try:
            api.archived_cleanup_candidates(archive, 0)
        except ValueError:
            pass
        else:
            raise AssertionError("days=0 should raise ValueError")

        print("OK")
    finally:
        shutil.rmtree(root)


if __name__ == "__main__":
    main()
