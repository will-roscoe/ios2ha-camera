"""Choosing the archive's stills for a timelapse: by the times in their paths.

The archive is `YYYY/MM/DD/HHMMSS.jpg` in the service's local time, written by
`Archive.write_still`, and is never pruned (Will: "we shouldnt be pruning archive").
"""
import os
import time

import pytest

from ios2ha_camera import timelapse
from ios2ha_camera.archive import Archive
from ios2ha_camera.timelapse import SpecError, select_stills, still_time

T0 = time.mktime((2026, 9, 27, 22, 0, 0, 0, 0, -1))      # local 22:00, a day before midnight


@pytest.fixture
def archive(tmp_path):
    a = Archive(str(tmp_path / "archive"))
    times = [T0 + i * 600 for i in range(30)]               # every 10 min, past midnight
    paths = [a.write_still(b"x", t) for t in times]
    day = os.path.dirname(paths[0])
    open(os.path.join(day, "230000.jpg.tmp"), "wb").close()
    open(os.path.join(day, "notes.txt"), "wb").close()
    return a.root, times, paths


def test_a_path_gives_its_time(archive):
    _, times, paths = archive
    assert still_time(paths[3]) == times[3]
    assert still_time("/x/2026/09/27/notes.txt") is None


def test_the_span_is_inclusive_and_crosses_midnight(archive):
    root, times, paths = archive
    assert select_stills(root, times[2], times[20], 1) == paths[2:21]


def test_every_nth_from_the_first(archive):
    root, times, paths = archive
    assert select_stills(root, times[0], times[29], 3) == paths[0:30:3]


def test_nothing_in_the_span_is_an_empty_list(archive):
    root, times, _ = archive
    assert select_stills(root, times[-1] + 3600, times[-1] + 7200, 1) == []


def test_too_many_stills_says_what_every_would_fit(archive, monkeypatch):
    root, times, _ = archive
    monkeypatch.setattr(timelapse, "MAX_FRAMES", 7)
    with pytest.raises(SpecError, match="every 5"):
        select_stills(root, times[0], times[29], 1)


def test_a_missing_archive_is_empty(tmp_path):
    assert select_stills(str(tmp_path / "none"), T0, T0 + 3600, 1) == []
