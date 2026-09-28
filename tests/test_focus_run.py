"""The service's own focus runs: on a button, and once each dusk.

Driven against a synthetic camera whose picture sharpens as its lens nears a hidden
best position, so a run is exercised end to end -- moves, frames, averaging, scoring,
the search -- with the phone's failures injected where they happen.
"""
import contextlib
import io
import time

import cv2
import numpy as np
import pytest
from PIL import Image
from tests.test_supervisor import FakeArchive, FakeMqtt, Inline

from ios2ha_camera.ring_buffer import Frame, RingBuffer
from ios2ha_camera.settings import Settings
from ios2ha_camera.supervisor import Supervisor


def _view(seed=0, h=240, w=320):
    rng = np.random.default_rng(seed)
    img = np.full((h, w), 20.0, np.float32)
    for x0 in range(40, 280, 40):
        img[100:, x0:x0 + 30] = 60.0
        for wy in range(110, 230, 12):
            if rng.random() < 0.6:
                img[wy:wy + 5, x0 + 5:x0 + 12] = 200.0
    return img


class LensCamera:
    """The phone's imaging service and its picture, as far as a focus run sees them."""

    def __init__(self, best=0.78, position=0.70, mode="MANUAL", flat=False, fail_after=None,
                 asleep=False, trend=0.0):
        self.best, self.position, self.mode = best, position, mode
        self.flat, self.fail_after = flat, fail_after
        # Backgrounded: iOS has suspended the app's socket, so every call is refused
        # until a session wakes it -- which is what the battery policy leaves it as.
        self.asleep = asleep
        # Contrast rising with every batch of frames, as lights come on at dusk.
        self.trend, self.batches = trend, 0
        self.moves = []
        self._view = np.full((240, 320), 30.0, np.float32) if flat else _view()

    def _awake(self):
        if self.asleep:
            raise ConnectionRefusedError(111, "Connection refused")

    def get_autofocus(self):
        self._awake()
        return self.mode

    def set_autofocus(self, mode):
        self._awake()
        self.mode = mode
        return True

    def focus_position(self):
        return self.position

    def move_focus(self, position):
        self._awake()
        if self.fail_after is not None and len(self.moves) >= self.fail_after:
            self.fail_after = None           # answers again afterwards, for the restore
            raise ConnectionRefusedError(111, "Connection refused")
        self.moves.append(position)
        self.position = position
        return True

    def jpeg(self, rng):
        sigma = 0.6 + 25 * abs(self.position - self.best)
        contrast = 1 + self.trend * self.batches
        view = 20 + (self._view - 20) * contrast
        img = cv2.GaussianBlur(view, (0, 0), sigma) + rng.normal(0, 2, self._view.shape)
        b = io.BytesIO()
        Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).convert("RGB").save(b, "JPEG", quality=92)
        return b.getvalue()


class CameraUpstream:
    """Frames on demand, taken at whatever the lens is doing when asked."""

    def __init__(self, buf, camera, deliver=True):
        self.buf, self.camera, self.deliver = buf, camera, deliver
        self.rng = np.random.default_rng(5)

    @contextlib.contextmanager
    def hold(self, reason, linger_s=0.0):
        yield

    def wait_for_frames(self, n, since, timeout):
        # A session delivering frames is a woken app.
        if self.deliver:
            self.camera.asleep = False
            self.camera.batches += 1
            for _ in range(n):
                self.buf.append(Frame(self.camera.jpeg(self.rng), max(time.time(), since)))
        return n

    def session_start_ts(self):
        return None


def _sup(camera, deliver=True, **settings):
    s = Settings()
    for k, v in settings.items():
        setattr(s, k, v)
    buf = RingBuffer(200_000_000)
    return Supervisor(s, buf, FakeMqtt(), FakeArchive(), onvif=camera,
                      upstream=CameraUpstream(buf, camera, deliver),
                      device_executor=Inline(), sleep=lambda s: None)


# -- a run -----------------------------------------------------------------------

def test_the_button_runs_on_the_capture_thread_not_the_caller():
    sup = _sup(LensCamera())
    sup.handle_control("focus_now", "PRESS")
    assert sup._focus_requested and sup.capture_requested.is_set()


def test_a_run_finds_the_sharpest_position_and_holds_it():
    camera = LensCamera(best=0.78)
    sup = _sup(camera, autofocus=True)
    assert sup.run_focus("button") == "focused"
    assert sup.s.focus_position == pytest.approx(0.78, abs=0.02)
    assert sup.s.autofocus is False
    assert camera.mode == "MANUAL" and camera.position == sup.s.focus_position
    assert sup.mqtt.published["focus_run"] == "focused"
    assert sup.mqtt.published["focus_position"] == sup.s.focus_position


def test_no_clear_peak_changes_nothing_and_puts_the_lens_back():
    camera = LensCamera(flat=True, position=0.8)
    sup = _sup(camera, autofocus=False, focus_position=0.8)
    assert sup.run_focus("button") == "unchanged_no_clear_peak"
    assert sup.s.focus_position == 0.8
    assert camera.position == 0.8


def test_a_run_that_fails_from_autofocus_leaves_autofocus_on():
    camera = LensCamera(flat=True, mode="AUTO")
    sup = _sup(camera, autofocus=True)
    sup.run_focus("button")
    assert camera.mode == "AUTO" and sup.s.autofocus is True


def test_a_phone_that_stops_answering_mid_run_is_reported_and_the_lens_restored():
    camera = LensCamera(position=0.8, fail_after=3)
    sup = _sup(camera, autofocus=False, focus_position=0.8)
    assert sup.run_focus("button") == "failed_phone"
    assert camera.position == 0.8
    assert sup.s.focus_position == 0.8


def test_no_frames_is_reported_and_the_lens_restored():
    camera = LensCamera(position=0.8)
    sup = _sup(camera, deliver=False, autofocus=False, focus_position=0.8)
    assert sup.run_focus("button") == "failed_no_frames"
    assert camera.position == 0.8


def test_a_session_opening_during_a_run_does_not_move_the_lens():
    """The run holds the phone, which opens a session, which re-applies the held
    focus -- that must not land in the middle of the sweep."""
    camera = LensCamera()
    sup = _sup(camera, autofocus=False, focus_position=0.8)
    with sup._lens_lock:              # a run owns the lens
        sup.on_session_open()
    assert camera.moves == []


def test_a_run_waits_for_a_focus_already_being_applied():
    """An apply already past its check -- a viewer's session waking the app, say --
    would otherwise move the lens under the first probes."""
    import threading
    camera = LensCamera(best=0.78)
    sup = _sup(camera, autofocus=False, focus_position=0.9)
    gate, entered = threading.Event(), threading.Event()
    real_move = camera.move_focus

    def slow_first_move(p):
        if not entered.is_set():
            entered.set()
            gate.wait(5)
        return real_move(p)
    camera.move_focus = slow_first_move
    applying = threading.Thread(target=sup._apply_focus)
    applying.start()
    assert entered.wait(5)
    running = threading.Thread(target=sup.run_focus, args=("button",))
    running.start()
    time.sleep(0.2)
    assert camera.moves == [], "the run must not start while the apply is moving the lens"
    gate.set()
    applying.join(5); running.join(30)
    assert camera.moves[:2] == [0.93, 0.9], "the apply finishes first"
    assert sup._focus_state == "focused"


# -- the phone asleep, and the light changing --------------------------------------

def test_a_run_wakes_a_backgrounded_app_before_asking_it_anything():
    """At dusk the battery policy has usually backgrounded the app: every call is
    refused until the session the run opens has woken it."""
    camera = LensCamera(best=0.78, asleep=True)
    sup = _sup(camera, autofocus=False, focus_position=0.9)
    assert sup.run_focus("dusk") == "focused"
    assert sup.s.focus_position == pytest.approx(0.78, abs=0.02)


def test_a_run_that_only_found_a_trend_keeps_the_better_focus_it_had():
    """Contrast rising through the run -- lights coming on -- makes each nearer probe
    score higher and can fake a peak at the near end. The result has to beat the
    position it would replace, measured then and there."""
    camera = LensCamera(best=0.80, trend=0.35)
    sup = _sup(camera, autofocus=False, focus_position=0.80)
    result = sup.run_focus("dusk")
    assert result == "unchanged_already_sharpest"
    assert sup.s.focus_position == 0.80
    assert camera.position == 0.80


def test_the_run_state_is_published_from_the_start():
    sup = _sup(LensCamera())
    sup.upstream = None          # health reads more of the upstream than a run needs
    assert sup.health_state()["focus_run"] == "idle"


# -- the dusk run ----------------------------------------------------------------

EVENING = time.mktime((2026, 9, 29, 19, 0, 0, 0, 0, -1))
DAWN = time.mktime((2026, 9, 30, 6, 40, 0, 0, 0, -1))


@pytest.mark.parametrize("weight, due", [(None, False), (0.0, False), (0.2, True),
                                         (0.49, True), (0.5, False), (1.0, False)])
def test_the_dusk_run_waits_for_the_light_to_start_falling(weight, due):
    sup = _sup(LensCamera(), autofocus=False)
    sup._prev_weight, sup._last_weight = 0.0, weight
    assert sup._dusk_focus_due(EVENING) is due


def test_dawn_is_not_dusk():
    """The light rises back through the same band at dawn; that is not the
    evening run, and must not use up the evening's."""
    sup = _sup(LensCamera(), autofocus=False)
    sup._prev_weight, sup._last_weight = 0.4, 0.3        # getting lighter
    assert not sup._dusk_focus_due(DAWN)
    sup._prev_weight, sup._last_weight = 0.2, 0.3        # getting darker, but morning
    assert not sup._dusk_focus_due(DAWN)
    sup._prev_weight, sup._last_weight = 0.2, 0.3
    assert sup._dusk_focus_due(EVENING)


def test_the_dusk_run_leaves_the_phones_own_autofocus_alone():
    sup = _sup(LensCamera(), autofocus=True)
    sup._prev_weight, sup._last_weight = 0.1, 0.2
    assert sup._dusk_focus_due(EVENING) is False


def test_the_dusk_run_can_be_switched_off():
    sup = _sup(LensCamera(), autofocus=False, focus_at_dusk=False)
    sup._prev_weight, sup._last_weight = 0.1, 0.2
    assert sup._dusk_focus_due(EVENING) is False


def test_one_successful_dusk_run_a_day_and_three_tries_at_most():
    sup = _sup(LensCamera(), autofocus=False)
    sup._prev_weight, sup._last_weight = 0.1, 0.2
    day = time.mktime((2026, 9, 29, 19, 0, 0, 0, 0, -1))
    for _ in range(3):
        assert sup._dusk_focus_due(day)
        sup._dusk_ran("unchanged_no_clear_peak")
    assert not sup._dusk_focus_due(day)
    assert sup._dusk_focus_due(day + 86400), "a new day, a new budget"
    sup._dusk_ran("focused")
    assert not sup._dusk_focus_due(day + 86400)


def test_a_failed_still_does_not_leave_a_stale_light_level_behind():
    """The dusk decision reads the last still's light: a still that failed has none."""
    sup = _sup(LensCamera(), deliver=False, autofocus=False)
    sup._last_weight = 0.2
    sup.produce_still(now_fps=12.0)
    assert sup._last_weight is None
