"""Where the camera is, for timelapses picked by the sun.

Controls like any other -- writable from Home Assistant, MQTT, the API or the page,
and kept across restarts -- so every interface can ask for sunrise picks.
"""
import math

import pytest

from ios2ha_camera.settings import Settings


def test_unset_until_given():
    assert Settings().location() is None


def test_given_it_is_a_place():
    s = Settings()
    s.apply_control("latitude", 51.5074)
    s.apply_control("longitude", -0.1278)
    assert s.location() == (51.5074, -0.1278)


@pytest.mark.parametrize("name, value", [("latitude", 91), ("latitude", -90.5),
                                         ("longitude", 181), ("longitude", "abc"),
                                         ("latitude", math.nan), ("latitude", None)])
def test_a_place_that_is_not_one_is_refused(name, value):
    with pytest.raises(ValueError, match=name):
        Settings().apply_control(name, value)


def test_from_the_environment():
    assert Settings.from_env({"LATITUDE": "51.5", "LONGITUDE": "-0.12"}).location() == (51.5, -0.12)
