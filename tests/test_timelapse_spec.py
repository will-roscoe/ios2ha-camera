"""The timelapse build spec: one JSON object, whichever interface sends it.

Will: builds are made on request, "possibly sends with a json content that specifies
things like fps, etc for later extensibility". Unknown keys are refused, so a typo
cannot silently build the wrong thing.
"""
import time
from datetime import datetime

import pytest

from ios2ha_camera.timelapse import RANGES, SpecError, parse_spec

NOW = 1_791_000_000.0      # 2026-10-03, after the spans below


def test_a_range_needs_nothing_else():
    s = parse_spec({"range": "7d"}, NOW)
    assert (s.name, s.kind, s.range) == ("last_7d", "range", "7d")
    assert (s.start, s.end) == (NOW - 604_800, NOW)
    assert (s.fps, s.width, s.every, s.quality) == (None, None, None, 23)   # fitted later


def test_the_ranges():
    assert RANGES == {"12h": 43_200, "24h": 86_400, "7d": 604_800, "1mo": 2_592_000}


def test_a_custom_span_is_named_by_its_local_times():
    s = parse_spec({"start": "2026-09-28T18:00:00+00:00", "end": "2026-09-29T06:00:00+00:00"}, NOW)
    assert s.kind == "custom" and s.range is None
    start = datetime.fromisoformat("2026-09-28T18:00:00+00:00").timestamp()
    end = datetime.fromisoformat("2026-09-29T06:00:00+00:00").timestamp()
    assert (s.start, s.end) == (start, end)
    stamp = lambda t: time.strftime("%Y%m%d-%H%M", time.localtime(t))   # noqa: E731
    assert s.name == f"custom_{stamp(start)}_{stamp(end)}"


def test_a_time_without_a_zone_is_the_services_local_time():
    s = parse_spec({"start": "2026-09-28T18:00:00", "end": "2026-09-28T19:00:00"}, NOW)
    assert s.start == time.mktime(time.strptime("2026-09-28 18:00:00", "%Y-%m-%d %H:%M:%S"))


def test_the_end_defaults_to_now():
    assert parse_spec({"start": "2026-09-28T18:00:00+00:00"}, NOW).end == NOW


def test_every_option_is_taken():
    s = parse_spec({"range": "24h", "fps": 30, "width": 1280, "every": 2, "quality": 28,
                    "version": 1}, NOW)
    assert (s.fps, s.width, s.every, s.quality) == (30, 1280, 2, 28)


def test_a_custom_build_can_be_named():
    assert parse_spec({"start": "2026-09-28T18:00:00+00:00", "name": "storm-28_sep"},
                      NOW).name == "storm-28_sep"


@pytest.mark.parametrize("value, key", [
    ("7d", "object"),
    ([1], "object"),
    ({"range": "7d", "colour": "red"}, "colour"),
    ({"range": "7d", "start": "2026-09-28T18:00:00"}, "range"),
    ({}, "range"),
    ({"range": "9d"}, "range"),
    ({"start": "2026-09-29T18:00:00", "end": "2026-09-28T18:00:00"}, "start"),
    ({"start": "yesterday"}, "start"),
    ({"range": "7d", "fps": 0}, "fps"),
    ({"range": "7d", "fps": 61}, "fps"),
    ({"range": "7d", "fps": "15"}, "fps"),
    ({"range": "7d", "fps": True}, "fps"),
    ({"range": "7d", "fps": 15.5}, "fps"),
    ({"range": "7d", "width": 159}, "width"),
    ({"range": "7d", "width": 3841}, "width"),
    ({"range": "7d", "width": 1281}, "width"),
    ({"range": "7d", "every": 0}, "every"),
    ({"range": "7d", "every": 1001}, "every"),
    ({"range": "7d", "quality": 14}, "quality"),
    ({"range": "7d", "quality": 41}, "quality"),
    ({"start": "2026-09-28T18:00:00", "name": "Bad"}, "name"),
    ({"start": "2026-09-28T18:00:00", "name": "a/b"}, "name"),
    ({"start": "2026-09-28T18:00:00", "name": "../x"}, "name"),
    ({"start": "2026-09-28T18:00:00", "name": "x" * 49}, "name"),
    ({"start": "2026-09-28T18:00:00", "name": "last_7d"}, "name"),
    ({"range": "7d", "name": "mine"}, "name"),
    ({"range": "7d", "version": 2}, "version"),
])
def test_what_is_refused_says_which_key(value, key):
    with pytest.raises(SpecError, match=key):
        parse_spec(value, NOW)


@pytest.mark.parametrize("name", ["storm\n", "storm\r", "\nstorm"])
def test_a_name_is_nothing_but_its_characters(name):
    """`$` would let a trailing newline through, and a file named with one can never
    be fetched."""
    with pytest.raises(SpecError, match="name"):
        parse_spec({"start": "2026-09-28T18:00:00", "name": name}, NOW)
