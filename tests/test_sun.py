"""Sunrise, sunset and solar noon, for picking a timelapse's stills by the sun.

Checked against published times for London (51.5074 N, 0.1278 W): within a few
minutes is all a still every ten minutes can use.
"""
import calendar

import pytest

from ios2ha_camera.sun import sun_event

LONDON = (51.5074, -0.1278)


def _utc(y, mo, d, h, mi):
    return calendar.timegm((y, mo, d, h, mi, 0))


@pytest.mark.parametrize("day, event, expected", [
    ((2026, 6, 21), "sunrise", (3, 43)),
    ((2026, 6, 21), "sunset", (20, 21)),
    ((2026, 12, 21), "sunrise", (8, 4)),
    ((2026, 12, 21), "sunset", (15, 54)),
    ((2026, 9, 28), "sunrise", (5, 58)),
    ((2026, 9, 28), "sunset", (17, 44)),
    ((2026, 6, 21), "noon", (12, 2)),
])
def test_london(day, event, expected):
    got = sun_event(*day, *LONDON, event)
    assert abs(got - _utc(*day, *expected)) <= 4 * 60, (event, got - _utc(*day, *expected))


def test_no_sunset_in_the_arctic_summer():
    assert sun_event(2026, 6, 21, 78.22, 15.65, "sunset") is None
    assert sun_event(2026, 12, 21, 78.22, 15.65, "sunrise") is None


def test_an_unknown_event_is_refused():
    with pytest.raises(ValueError):
        sun_event(2026, 6, 21, *LONDON, "dusk")
