"""Auto horizon in the service: a button levels the stills, and the rotation is applied
in its place -- after the flat field, which is fixed to the sensor's pixels, and before
anything measured on the view, whose regions are drawn on the levelled still.

Driven through the real stack and render path with real JPEG frames.
"""
import io
import time

import numpy as np
import pytest
from PIL import Image
from tests.test_horizon import _city, _rolled
from tests.test_supervisor import FakeArchive, FakeMqtt, Inline

from ios2ha_camera import horizon, render
from ios2ha_camera.colour_ref import ColourReference
from ios2ha_camera.ring_buffer import Frame, RingBuffer
from ios2ha_camera.settings import Settings
from ios2ha_camera.supervisor import Supervisor

DAY, NIGHT = 0.1, 250.0         # target levels that make the same view day or night


def _fill(buf, view, n=6):
    rng = np.random.default_rng(1)
    now = time.time()
    for i in range(n):
        img = np.clip(view.astype(np.float32) + rng.normal(0, 1.5, view.shape), 0, 255)
        b = io.BytesIO()
        Image.fromarray(img.astype(np.uint8)).save(b, "JPEG", quality=92)
        buf.append(Frame(b.getvalue(), now - (n - 1 - i) / 12))


def _sup(tmp_path=None, **settings):
    s = Settings()
    s.stacking_mode, s.fixed_depth = "fixed", 6
    for k, v in settings.items():
        setattr(s, k, v)
    buf = RingBuffer(50_000_000)
    extra = {}
    if tmp_path is not None:
        extra["colour"] = ColourReference(str(tmp_path / "colour.npz"))
    sup = Supervisor(s, buf, FakeMqtt(), FakeArchive(), device_executor=Inline(),
                     sleep=lambda s: None, **extra)
    return sup, buf


def _still(sup):
    return np.asarray(Image.open(io.BytesIO(sup.last_still_jpeg)).convert("RGB"))


def test_the_button_levels_a_rolled_view():
    sup, buf = _sup(target_level=DAY)
    _fill(buf, _rolled(_city(), 1.5))
    sup.produce_still(now_fps=12.0)
    sup.handle_control("level_horizon", "PRESS")
    assert sup.s.rotation == pytest.approx(1.5, abs=0.2)
    assert sup.mqtt.published["rotation"] == sup.s.rotation
    assert sup.mqtt.published["horizon"] == "levelled"
    sup.produce_still(now_fps=12.0)
    assert abs(horizon.estimate_tilt(_still(sup))) < 0.3


def test_pressing_again_on_a_level_view_keeps_the_rotation():
    """The estimate is taken on the still as shown -- already turned by the rotation --
    so a second press finds what is left, not the whole roll again."""
    sup, buf = _sup(target_level=DAY)
    _fill(buf, _rolled(_city(), 1.5))
    sup.produce_still(now_fps=12.0)
    sup.handle_control("level_horizon", "PRESS")
    first = sup.s.rotation
    assert first != 0.0
    sup.produce_still(now_fps=12.0)
    sup.handle_control("level_horizon", "PRESS")
    assert sup.s.rotation == pytest.approx(first, abs=0.2)


def test_no_still_yet_is_refused():
    sup, _ = _sup(target_level=DAY)
    sup.handle_control("level_horizon", "PRESS")
    assert sup.mqtt.published["horizon"] == "rejected_no_still"
    assert sup.s.rotation == 0.0


def test_a_night_still_is_refused():
    """Measured on 28 Sep: night stills' verticals include reflections in the glass,
    and one read 1.4 degrees off the daylight answer while passing every check."""
    sup, buf = _sup(target_level=NIGHT)
    _fill(buf, _rolled(_city(), 1.5))
    sup.produce_still(now_fps=12.0)
    sup.handle_control("level_horizon", "PRESS")
    assert sup.mqtt.published["horizon"] == "rejected_night"
    assert sup.s.rotation == 0.0


def test_a_view_with_no_clear_lines_is_refused():
    sup, buf = _sup(target_level=DAY, rotation=0.6)
    rng = np.random.default_rng(3)
    _fill(buf, rng.integers(60, 200, (540, 960, 3)).astype(np.uint8))
    sup.produce_still(now_fps=12.0)
    sup.handle_control("level_horizon", "PRESS")
    assert sup.mqtt.published["horizon"] == "rejected_no_clear_lines"
    assert sup.s.rotation == 0.6


class _RecordingFlat:
    ready = True

    def __init__(self):
        self.seen = None

    def correct(self, stacked, weight):
        self.seen = stacked.copy()
        return stacked, None, 1.0


def test_the_flat_field_is_taken_off_before_the_view_is_turned():
    """The flat field's pattern is fixed to the sensor's pixels."""
    flat = _RecordingFlat()
    sup, buf = _sup(target_level=NIGHT, rotation=3.0)
    sup.flat = flat
    _fill(buf, _city())
    sup.produce_still(now_fps=12.0)
    assert np.array_equal(flat.seen, sup.last_stack)


def test_the_colour_reference_is_recorded_from_the_levelled_view(tmp_path):
    """The colour region is drawn on the still, which is levelled."""
    sup, buf = _sup(tmp_path, target_level=NIGHT, rotation=3.0)
    seen = {}
    record = sup.colour.record

    def spy(linear, region):
        seen["linear"] = linear
        return record(linear, region)

    sup.colour.record = spy
    _fill(buf, _city())
    sup.produce_still(now_fps=12.0)
    sup.handle_control("set_colour_reference", "PRESS")
    levelled = horizon.level(sup.last_stack, 3.0)
    black, _ = render.calibration(levelled, sup.s)
    assert np.allclose(seen["linear"], np.clip(levelled - black, 0, None))


def test_the_state_is_published_from_the_start():
    sup, _ = _sup()
    assert sup.health_state()["horizon"] == "none"
