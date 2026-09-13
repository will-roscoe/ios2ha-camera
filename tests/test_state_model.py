"""The 1.2.0 state model: renamed states, active sub-states, and the booleans.

The point of the release is that "the camera is not sending frames" was one word
covering four unrelated situations — a dead cable, a phone off the network, an app
iOS suspended, and an app whose Start streaming button was never tapped. Only the
last two are recoverable without a person, and only one of them is a fault.
"""

import time

import pytest

from ios2ha_camera.mqtt_io import discovery_payloads
from ios2ha_camera.ring_buffer import Frame, RingBuffer
from ios2ha_camera.settings import Settings
from ios2ha_camera.states import ACTIVE_STATES, STREAM_STATES, classify_probe_failure
from ios2ha_camera.upstream import Upstream


class FakeIngestor:
    def __init__(self):
        self.started = 0

    def start(self):
        self.started += 1

    def stop(self, timeout=None):
        return True


def _settings(**kw):
    s = Settings()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _up(probe=None, **kw):
    buf = RingBuffer(1 << 20)
    up = Upstream(FakeIngestor(), buf, _settings(**kw),
                  probe=probe or (lambda: Frame(b"jpeg", 1000.0)))
    return up, buf


# -- classifying why a connection failed ----------------------------------------

def test_a_refusal_is_backgrounded_not_unavailable():
    """Measured 2026-09-13: a backgrounded app refuses immediately while the phone
    still answers ICMP. iOS suspends the listening socket, not the process."""
    assert classify_probe_failure(ConnectionRefusedError(111, "Connection refused")) \
        == "backgrounded"


@pytest.mark.parametrize("exc", [
    TimeoutError("timed out"),
    OSError(113, "No route to host"),
    ConnectionResetError(104, "Connection reset by peer"),
    RuntimeError("HTTP 500"),
])
def test_everything_else_is_unavailable(exc):
    """A phone genuinely off the network cannot refuse anything, so only an
    outright refusal may be read as a deliberate pause."""
    assert classify_probe_failure(exc) == "unavailable"


# -- the active sub-states ------------------------------------------------------

def _streaming(up, buf, t=1000.0):
    """Put the upstream into a state where frames are flowing."""
    up.tick(now=t)
    buf.append(Frame(b"jpeg", t + 0.5))
    return t + 1.0


def test_a_capture_hold_reports_capturing():
    up, buf = _up()
    with up.hold("capture"):
        now = _streaming(up, buf)
        assert up.state(now=now) == "capturing"


def test_a_viewer_hold_reports_in_use():
    up, buf = _up()
    with up.hold("viewer"):
        now = _streaming(up, buf)
        assert up.state(now=now) == "in_use"


def test_a_viewer_wins_over_a_capture():
    """Both can hold at once. A viewer is a person waiting on the picture; a
    capture is a background job that finishes either way."""
    up, buf = _up()
    with up.hold("capture"), up.hold("viewer"):
        now = _streaming(up, buf)
        assert up.state(now=now) == "in_use"


def test_frames_with_nobody_holding_report_plain_streaming():
    """Reachable with on_demand off, where the connection is simply always open,
    so it keeps the name the state had before this release."""
    up, buf = _up(on_demand=False)
    now = _streaming(up, buf)
    assert up.state(now=now) == "streaming"


def test_every_active_substate_is_a_declared_state():
    for s in ACTIVE_STATES:
        assert s in STREAM_STATES


# -- the booleans ---------------------------------------------------------------

class FakeUpstream:
    def __init__(self, state, holders=()):
        self._state, self._holders = state, holders

    def state(self, now=None):
        return self._state

    def holders(self, now=None):
        return self._holders

    def camera_online(self, now=None):
        return self._state in ACTIVE_STATES + ("ready",)

    def duty_1h(self, now=None):
        return 0.0

    def tick_age(self, now=None):
        return 0.1

    def session_start_ts(self):
        return time.time()


def _health(state, holders=(), cable="ok", app_running=None):
    from tests.test_supervisor import FakeArchive, FakeMqtt, Inline

    from ios2ha_camera.supervisor import Supervisor
    sup = Supervisor(Settings(), RingBuffer(1 << 20), FakeMqtt(), FakeArchive(),
                     upstream=FakeUpstream(state, holders), device_executor=Inline())
    sup._cable_state = cable
    sup._app_running = app_running
    return sup.health_state()


@pytest.mark.parametrize("state,expected", [
    ("in_use", True), ("capturing", True), ("streaming", True), ("ready", True),
    # A refusal proves the phone's IP stack answered, so WiFi is up even though
    # the app is not listening. This is the distinction the release exists for.
    ("backgrounded", True),
    ("streaming_off", True),
    ("unavailable", False),
])
def test_reachable_over_wifi(state, expected):
    assert _health(state)["reachable_over_wifi"] is expected


@pytest.mark.parametrize("cable,expected", [
    ("ok", True), ("no_device", False), ("usbmuxd_down", False),
])
def test_reachable_over_cable_follows_cable_state(cable, expected):
    assert _health("ready", cable=cable)["reachable_over_cable"] is expected


@pytest.mark.parametrize("state,expected", [
    ("in_use", True), ("capturing", True), ("streaming", True), ("ready", True),
    # Reachable, but not willing: the app is suspended, or Start streaming was
    # never tapped. Neither can be fixed by connecting harder.
    ("backgrounded", False),
    ("streaming_off", False),
    ("unavailable", False),
])
def test_app_streaming(state, expected):
    assert _health(state)["app_streaming"] is expected


def test_app_running_is_unknown_until_the_cable_answers():
    """Only the cable can tell a suspended app from a killed one. With no answer
    the honest value is None, published as unknown rather than guessed as False —
    a false "app not running" would send someone to relaunch a healthy app."""
    assert _health("backgrounded", app_running=None)["app_running"] is None
    assert _health("backgrounded", app_running=True)["app_running"] is True
    assert _health("backgrounded", app_running=False)["app_running"] is False


def test_capturing_and_being_viewed_follow_the_holders():
    d = _health("capturing", holders=("capture",))
    assert d["capturing_stills"] is True and d["being_viewed"] is False

    d = _health("in_use", holders=("viewer",))
    assert d["capturing_stills"] is False and d["being_viewed"] is True

    d = _health("in_use", holders=("capture", "viewer"))
    assert d["capturing_stills"] is True and d["being_viewed"] is True

    d = _health("ready", holders=())
    assert d["capturing_stills"] is False and d["being_viewed"] is False


def test_a_lingering_viewer_still_counts_as_being_viewed():
    """The connection is still open and still costing battery during the linger,
    so a dashboard that showed nobody viewing would be misleading."""
    d = _health("in_use", holders=("viewer (linger)",))
    assert d["being_viewed"] is True


# -- discovery ------------------------------------------------------------------

def test_every_boolean_is_declared_to_home_assistant():
    published = set(_health("ready").keys())
    binaries = {t.split("/")[3] for t, _ in discovery_payloads(Settings())
                if t.startswith("homeassistant/binary_sensor/")}
    for name in ("reachable_over_cable", "reachable_over_wifi", "app_running",
                 "app_streaming", "capturing_stills", "being_viewed"):
        assert name in binaries, f"{name} has no discovery payload"
        assert name in published, f"{name} is declared but never published"


@pytest.mark.parametrize("state", STREAM_STATES)
def test_every_reachable_state_is_declared(state):
    """Home Assistant silently marks an enum sensor unavailable when it receives
    a state outside its options, so a value the code can produce but discovery
    does not list fails invisibly."""
    cfg = next(c for _, c in discovery_payloads(Settings())
               if c["default_entity_id"] == "sensor.ios2ha_camera_stream_state")
    assert state in cfg["options"]
