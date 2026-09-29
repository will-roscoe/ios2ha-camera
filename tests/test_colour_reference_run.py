"""The colour reference in the service: recorded by a button, applied to night stills.

Driven through the real stack and render path with real JPEG frames of a synthetic
night view, so what is checked is what a still actually gets.
"""
import io
import time

import numpy as np
from PIL import Image
from tests.test_supervisor import FakeArchive, FakeMqtt, Inline

from ios2ha_camera.colour_ref import ColourReference
from ios2ha_camera.ring_buffer import Frame, RingBuffer
from ios2ha_camera.settings import Settings
from ios2ha_camera.supervisor import Supervisor

REGION = (0.30, 0.55, 0.45, 0.80)


def _night_view(seed=0, h=270, w=480):
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.float32) + np.float32((3, 3, 4))
    for i, colour in enumerate([(30, 26, 20), (46, 38, 30), (22, 24, 28), (60, 56, 44)]):
        x0 = 30 + i * 110
        img[140:, x0:x0 + 90] = colour
        for wy in range(160, 250, 25):
            for wx in range(x0 + 10, x0 + 80, 20):
                if rng.random() < 0.5:
                    img[wy:wy + 8, wx:wx + 10] = (200, 150, 80)
    return img


def _fill(buf, view, cast=(1.0, 1.0, 1.0), n=12):
    rng = np.random.default_rng(1)
    now = time.time()
    for i in range(n):
        img = np.clip(view * np.float32(cast) + rng.normal(0, 1.5, view.shape), 0, 255)
        b = io.BytesIO()
        Image.fromarray(img.astype(np.uint8)).save(b, "JPEG", quality=92)
        buf.append(Frame(b.getvalue(), now - (n - 1 - i) / 12))


def _sup(tmp_path, **settings):
    s = Settings()
    s.stacking_mode, s.fixed_depth = "fixed", 8
    s.colour_region_x0, s.colour_region_y0, s.colour_region_x1, s.colour_region_y1 = REGION
    for k, v in settings.items():
        setattr(s, k, v)
    buf = RingBuffer(50_000_000)
    sup = Supervisor(s, buf, FakeMqtt(), FakeArchive(), device_executor=Inline(),
                     sleep=lambda s: None,
                     colour=ColourReference(str(tmp_path / "colour.npz")))
    return sup, buf


def test_the_button_records_the_last_night_still(tmp_path):
    sup, buf = _sup(tmp_path)
    _fill(buf, _night_view())
    sup.produce_still(now_fps=12.0)
    sup.handle_control("set_colour_reference", "PRESS")
    assert sup.colour.ready
    assert sup.mqtt.published["colour_reference"] == "ready"


def test_no_still_yet_is_refused(tmp_path):
    sup, _ = _sup(tmp_path)
    sup.handle_control("set_colour_reference", "PRESS")
    assert sup.mqtt.published["colour_reference"] == "rejected_no_still"
    assert not sup.colour.ready


def test_a_daylight_still_is_refused(tmp_path):
    """A sunlit colour forced onto night stills is not what the reference is for."""
    sup, buf = _sup(tmp_path, target_level=2.0)
    _fill(buf, _night_view() * 5 + 20)                      # bright: needs no brightening
    sup.produce_still(now_fps=12.0)
    sup.handle_control("set_colour_reference", "PRESS")
    assert sup.mqtt.published["colour_reference"] == "rejected_daylight"


def test_a_night_still_with_a_cast_gets_its_colour_back(tmp_path):
    sup, buf = _sup(tmp_path, white_balance="reference")
    _fill(buf, _night_view())
    sup.produce_still(now_fps=12.0)
    sup.handle_control("set_colour_reference", "PRESS")
    _fill(buf, _night_view(), cast=(0.8, 1.0, 1.35))       # the phone's white balance moved
    sup.produce_still(now_fps=12.0)
    p = sup.mqtt.published
    assert p["colour_reference"] == "applied"
    assert p["wb_gain_r"] > p["wb_gain_g"] > p["wb_gain_b"]
    assert p["wb_gain_r"] / p["wb_gain_b"] > 1.4


def test_other_white_balance_modes_leave_it_alone(tmp_path):
    sup, buf = _sup(tmp_path, white_balance="as-shot")
    _fill(buf, _night_view())
    sup.produce_still(now_fps=12.0)
    sup.handle_control("set_colour_reference", "PRESS")
    _fill(buf, _night_view(), cast=(0.8, 1.0, 1.35))
    sup.produce_still(now_fps=12.0)
    p = sup.mqtt.published
    assert (p["wb_gain_r"], p["wb_gain_g"], p["wb_gain_b"]) == (1.0, 1.0, 1.0)
    assert p["colour_reference"] == "ready"


def test_a_view_that_no_longer_matches_is_left_as_shot(tmp_path):
    sup, buf = _sup(tmp_path, white_balance="reference")
    _fill(buf, _night_view())
    sup.produce_still(now_fps=12.0)
    sup.handle_control("set_colour_reference", "PRESS")
    _fill(buf, np.flipud(_night_view(seed=5)).copy(), cast=(0.8, 1.0, 1.35))
    sup.produce_still(now_fps=12.0)
    p = sup.mqtt.published
    assert p["colour_reference"] == "no_match"
    assert (p["wb_gain_r"], p["wb_gain_g"], p["wb_gain_b"]) == (1.0, 1.0, 1.0)


def test_the_state_is_published_from_the_start(tmp_path):
    sup, _ = _sup(tmp_path)
    assert sup.health_state()["colour_reference"] == "none"


def test_pressing_again_on_a_corrected_still_keeps_its_colour(tmp_path):
    """The colour a person approves is the one they saw -- corrected. Recording the
    stack under it would record the cast, and undo the correction."""
    sup, buf = _sup(tmp_path, white_balance="reference")
    _fill(buf, _night_view())
    sup.produce_still(now_fps=12.0)
    sup.handle_control("set_colour_reference", "PRESS")
    _fill(buf, _night_view(), cast=(0.8, 1.0, 1.35))
    sup.produce_still(now_fps=12.0)
    assert sup.mqtt.published["colour_reference"] == "applied"
    sup.handle_control("set_colour_reference", "PRESS")      # e.g. after moving the region
    _fill(buf, _night_view(), cast=(0.8, 1.0, 1.35))
    sup.produce_still(now_fps=12.0)
    p = sup.mqtt.published
    assert p["colour_reference"] == "applied"
    assert p["wb_gain_r"] / p["wb_gain_b"] > 1.4


def test_a_reference_that_fails_still_gives_a_still(tmp_path):
    sup, buf = _sup(tmp_path, white_balance="reference")
    _fill(buf, _night_view())
    sup.produce_still(now_fps=12.0)
    sup.handle_control("set_colour_reference", "PRESS")

    def broken(linear):
        raise KeyError("view")

    sup.colour.gains = broken
    before = len(sup.mqtt.stills)
    sup.produce_still(now_fps=12.0)
    assert len(sup.mqtt.stills) == before + 1
    p = sup.mqtt.published
    assert (p["wb_gain_r"], p["wb_gain_g"], p["wb_gain_b"]) == (1.0, 1.0, 1.0)


def test_daylight_is_not_reported_as_a_lost_match(tmp_path):
    """By day the reference is not used, so whether the view matches is not asked."""
    sup, buf = _sup(tmp_path, white_balance="reference")
    _fill(buf, _night_view())
    sup.produce_still(now_fps=12.0)
    sup.handle_control("set_colour_reference", "PRESS")
    sup.s.target_level = 2.0
    _fill(buf, np.flipud(_night_view(seed=5)).copy() * 5 + 20)
    sup.produce_still(now_fps=12.0)
    assert sup.mqtt.published["colour_reference"] == "ready"
