"""Which stills a timelapse uses, and how fast it plays them.

Will (2026-10-02): requests like "the last 50h", "a week at 6am", "a month of every
sunrise", "between two times"; and by default, "keeping length around 10s, and then
minimum fps of 20fps".
"""
import time

import pytest

from ios2ha_camera.archive import Archive
from ios2ha_camera.sun import sun_event
from ios2ha_camera.timelapse import SpecError, fit, parse_spec, pick_stills

NOW = 1_791_000_000.0
LONDON = (51.5074, -0.1278)


# -- the spec ---------------------------------------------------------------------

@pytest.mark.parametrize("text, seconds", [("50h", 180_000), ("90m", 5_400), ("3d", 259_200),
                                           ("2w", 1_209_600), ("1mo", 2_592_000)])
def test_a_duration_ends_now_and_is_rebuilt_under_its_own_name(text, seconds):
    s = parse_spec({"duration": text}, NOW)
    assert (s.start, s.end, s.name) == (NOW - seconds, NOW, f"last_{text}")


def test_a_duration_can_end_when_you_say():
    s = parse_spec({"duration": "2d", "end": "2026-09-03T00:00:00+00:00", "name": "sept"}, NOW)
    assert s.end - s.start == 172_800 and s.name == "sept"


def test_the_picks_are_read():
    assert parse_spec({"duration": "7d", "at": "06:00"}, NOW).at == ((6, 0),)
    assert parse_spec({"duration": "7d", "at": "06:00, 18:30"}, NOW).at == ((6, 0), (18, 30))
    assert parse_spec({"duration": "7d", "at": ["18:30", "06:00"]}, NOW).at == ((6, 0), (18, 30))
    assert parse_spec({"duration": "7d", "interval": "24h"}, NOW).interval == 86_400
    s = parse_spec({"duration": "1mo", "sun": "sunrise", "sun_offset": "+30m"}, NOW)
    assert (s.sun, s.sun_offset) == ("sunrise", 1_800)
    assert parse_spec({"duration": "1mo", "sun": "sunset", "sun_offset": "-1h"}, NOW).sun_offset == -3_600


def test_the_tolerance():
    assert parse_spec({"duration": "7d", "at": "06:00"}, NOW).tolerance == 900
    assert parse_spec({"duration": "1d", "interval": "10m"}, NOW).tolerance == 300
    assert parse_spec({"duration": "7d", "at": "06:00", "tolerance": "5m"}, NOW).tolerance == 300


def test_fps_and_every_are_chosen_unless_given():
    s = parse_spec({"range": "7d"}, NOW)
    assert (s.fps, s.every) == (None, None)
    s = parse_spec({"range": "7d", "fps": 24, "every": 3}, NOW)
    assert (s.fps, s.every) == (24, 3)


@pytest.mark.parametrize("value, key", [
    ({"duration": "50"}, "duration"),
    ({"duration": "50x"}, "duration"),
    ({"duration": "0h"}, "duration"),
    ({"duration": "-5h"}, "duration"),
    ({"duration": "7d", "range": "7d"}, "range"),
    ({"duration": "7d", "start": "2026-09-01T00:00:00"}, "start"),
    ({"duration": "7d", "at": "25:00"}, "at"),
    ({"duration": "7d", "at": "6"}, "at"),
    ({"duration": "7d", "at": "06:60"}, "at"),
    ({"duration": "7d", "at": []}, "at"),
    ({"duration": "7d", "interval": "0m"}, "interval"),
    ({"duration": "7d", "sun": "dusk"}, "sun"),
    ({"duration": "7d", "sun_offset": "+30m"}, "sun_offset"),
    ({"duration": "7d", "sun": "sunrise", "sun_offset": "30"}, "sun_offset"),
    ({"duration": "7d", "at": "06:00", "interval": "1h"}, "interval"),
    ({"duration": "7d", "at": "06:00", "sun": "sunrise"}, "sun"),
    ({"duration": "7d", "tolerance": "5m"}, "tolerance"),
    ({"duration": "7d", "name": "last_7d"}, "name"),
])
def test_what_is_refused_says_which_key(value, key):
    with pytest.raises(SpecError, match=key):
        parse_spec(value, NOW)


# -- how fast ----------------------------------------------------------------------

@pytest.mark.parametrize("n, expected", [
    (72, (1, 7.2)),        # 12h at one still in ten minutes: 10 s, under 20 fps
    (144, (1, 14.4)),      # 24h
    (250, (1, 25)),
    (301, (1, 30)),        # 10.03 s rather than halving the frames
    (1008, (4, 25)),       # 7d: 10.08 s
    (4320, (16, 27)),      # 1mo: 10.0 s
    (30, (1, 3.0)),        # a month of sunrises
    (5, (1, 0.5)),         # two seconds a still
])
def test_about_ten_seconds_first_then_twenty_fps(n, expected):
    assert fit(n) == expected


def test_every_count_lands_near_ten_seconds():
    """Ten seconds first: exactly, from fewer than 160 stills; within 8-12 s at 20-30
    fps from more."""
    for n in list(range(1, 2000)) + list(range(2000, 60_000, 37)):
        every, fps = fit(n)
        frames = -(-n // every)
        if n < 160:
            assert every == 1 and frames / fps == pytest.approx(10, abs=0.35), (n, fps)
        else:
            assert 8 <= frames / fps <= 12 and 20 <= fps <= 30, (n, every, fps)


def test_a_given_fps_still_keeps_about_ten_seconds():
    assert fit(1008, fps=60) == (2, 60)


def test_a_given_every_gets_the_fps_that_fits():
    assert fit(1008, every=2) == (2, 30)
    assert fit(300, every=3) == (3, 10.0)


def test_both_given_are_kept():
    assert fit(1008, fps=15, every=1) == (1, 15)


# -- which stills -------------------------------------------------------------------

def _archive(tmp_path, start, days, step=600, skip=None):
    a = Archive(str(tmp_path / "archive"))
    t, end = start, start + days * 86_400
    while t < end:
        if not (skip and skip[0] <= t < skip[1]):
            a.write_still(b"x", t)
        t += step
    return a.root


def _local(y, mo, d, h=0, mi=0):
    return time.mktime((y, mo, d, h, mi, 0, 0, 0, -1))


def test_an_interval_takes_the_still_nearest_each_step(tmp_path):
    start = _local(2026, 9, 1, 0, 3)                    # stills at :03, :13, ...
    root = _archive(tmp_path, start, 3)
    spec = parse_spec({"start": "2026-09-01T00:00:00", "end": "2026-09-03T23:59:00",
                       "interval": "24h"}, NOW)
    picked = pick_stills(spec, root)
    assert [p.rsplit("/", 2)[-2:] for p in picked] == [["01", "000300.jpg"], ["02", "000300.jpg"],
                                                       ["03", "000300.jpg"]]


def test_a_time_of_day_takes_one_still_a_day_and_skips_a_day_without_one(tmp_path):
    start = _local(2026, 9, 1)
    gap = (_local(2026, 9, 2, 5, 0), _local(2026, 9, 2, 7, 0))      # nothing near 06:00 on the 2nd
    root = _archive(tmp_path, start, 3, skip=gap)
    spec = parse_spec({"start": "2026-09-01T00:00:00", "end": "2026-09-04T00:00:00",
                       "at": "06:00"}, NOW)
    picked = pick_stills(spec, root)
    assert [p.rsplit("/", 2)[-2:] for p in picked] == [["01", "060000.jpg"], ["03", "060000.jpg"]]


def test_two_times_a_day_in_order(tmp_path):
    root = _archive(tmp_path, _local(2026, 9, 1), 2)
    spec = parse_spec({"start": "2026-09-01T00:00:00", "end": "2026-09-02T23:00:00",
                       "at": ["18:00", "06:00"]}, NOW)
    names = [p.rsplit("/", 2)[-2:] for p in pick_stills(spec, root)]
    assert names == [["01", "060000.jpg"], ["01", "180000.jpg"], ["02", "060000.jpg"],
                     ["02", "180000.jpg"]]


def test_the_sun_takes_the_still_nearest_each_sunrise(tmp_path):
    root = _archive(tmp_path, _local(2026, 9, 1), 3)
    spec = parse_spec({"start": "2026-09-01T00:00:00", "end": "2026-09-04T00:00:00",
                       "sun": "sunrise"}, NOW)
    picked = pick_stills(spec, root, location=LONDON)
    assert len(picked) == 3
    from ios2ha_camera.timelapse import still_time
    for day, path in zip((1, 2, 3), picked, strict=True):
        rise = sun_event(2026, 9, day, *LONDON, "sunrise")
        assert abs(still_time(path) - rise) <= 300


def test_the_sun_needs_to_know_where_it_is(tmp_path):
    root = _archive(tmp_path, _local(2026, 9, 1), 1)
    spec = parse_spec({"start": "2026-09-01T00:00:00", "end": "2026-09-02T00:00:00",
                       "sun": "sunset"}, NOW)
    with pytest.raises(SpecError, match="latitude"):
        pick_stills(spec, root, location=None)


def test_every_applies_after_the_picks(tmp_path):
    root = _archive(tmp_path, _local(2026, 9, 1), 4)
    spec = parse_spec({"start": "2026-09-01T00:00:00", "end": "2026-09-05T00:00:00",
                       "at": "12:00", "every": 2}, NOW)
    assert len(pick_stills(spec, root)) == 2


def test_two_targets_on_one_still_use_it_once(tmp_path):
    root = _archive(tmp_path, _local(2026, 9, 1), 1, step=3_600)
    spec = parse_spec({"start": "2026-09-01T00:00:00", "end": "2026-09-01T23:00:00",
                       "at": ["06:00", "06:10"]}, NOW)
    assert len(pick_stills(spec, root)) == 1
