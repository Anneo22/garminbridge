#!/usr/bin/env python3
"""Tests for route tag sidecar storage. Pure, no watch, no network."""
import argparse
import os
import tempfile

import api as API


def _args(key, tags="", folder=""):
    return argparse.Namespace(key=key, tags=tags, folder=folder)


def test_load_save_roundtrip_and_corrupt_file():
    d = tempfile.mkdtemp(prefix="gb_tags_")
    old = API.ROUTE_TAGS
    try:
        API.ROUTE_TAGS = os.path.join(d, "route_tags.json")
        assert API.load_tags() == {}
        API.save_tags({"b.fit": {"tags": ["tt", "gravel"], "folder": "Races"}})
        assert API.load_tags() == {"b.fit": {"tags": ["gravel", "tt"], "folder": "Races"}}
        open(API.ROUTE_TAGS, "w").write("{")
        assert API.load_tags() == {}
    finally:
        API.ROUTE_TAGS = old


def test_tags_entry_for_defaults_and_basename_lookup():
    store = {"SurreyHills.fit": {"tags": [" gravel ", "long", "gravel"], "folder": " Races "}}
    assert API.tags_entry_for("SurreyHills.fit", store) == {"tags": ["gravel", "long"], "folder": "Races"}
    assert API.tags_entry_for("course:73001", store) == {"tags": [], "folder": ""}
    assert API.tags_entry_for("missing.fit", store) == {"tags": [], "folder": ""}


def test_route_tags_set_adds_clears_and_rejects_paths():
    d = tempfile.mkdtemp(prefix="gb_tags_")
    old = API.ROUTE_TAGS
    try:
        API.ROUTE_TAGS = os.path.join(d, "route_tags.json")
        assert API.cmd_route_tags_set(_args("SurreyHills.fit", " gravel,tt,gravel ", " Races "))["ok"]
        assert API.load_tags() == {"SurreyHills.fit": {"tags": ["gravel", "tt"], "folder": "Races"}}
        assert not API.cmd_route_tags_set(_args("../SurreyHills.fit", "gravel", ""))["ok"]
        assert API.cmd_route_tags_set(_args("SurreyHills.fit", "", ""))["ok"]
        assert API.load_tags() == {}
    finally:
        API.ROUTE_TAGS = old


def test_enrich_keys_tags_by_backup_fit_basename_not_uid():
    lookup = [(API.content._norm("Surrey Hills loop"), "SurreyHills.fit",
               {"vb": "0 0 1 1", "d": "M0 0 L1 1"})]
    store = {"SurreyHills.fit": {"tags": ["gravel"], "folder": "Races"}}
    base = {
        "kind": "course",
        "name": "Surrey Hills loop",
        "id": 73001,
        "in_connect": True,
        "on_watch": True,
        "watch_file": "SURREYHI.FIT",
        "watch_idx": 1,
        "scheduled": False,
        "manifest_verified": True,
    }
    connect_uid = API.enrich(base, "cache", {"course"}, False, lookup, {}, (), store)
    watch_only = dict(base, id=None, in_connect=False)
    watch_uid = API.enrich(watch_only, "cache", {"course"}, False, lookup, {}, (), store)
    assert connect_uid["uid"] == "course:73001"
    assert watch_uid["uid"] == "course:w:SURREYHI.FIT"
    assert connect_uid["tag_key"] == watch_uid["tag_key"] == "SurreyHills.fit"
    assert connect_uid["tags"] == watch_uid["tags"] == ["gravel"]
    assert connect_uid["route_folder"] == watch_uid["route_folder"] == "Races"


if __name__ == "__main__":
    test_load_save_roundtrip_and_corrupt_file()
    test_tags_entry_for_defaults_and_basename_lookup()
    test_route_tags_set_adds_clears_and_rejects_paths()
    test_enrich_keys_tags_by_backup_fit_basename_not_uid()
    print("OK")
