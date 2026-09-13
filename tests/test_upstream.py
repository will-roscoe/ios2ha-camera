import logging
import threading
import time
import types
from urllib.parse import urlparse

import pytest
from tests.fakes.fake_camera import CREDENTIALS, FakeCamera

from ios2ha_camera.ingest import Ingestor
from ios2ha_camera.ring_buffer import Frame, RingBuffer
from ios2ha_camera.upstream import (
    ONLINE_IDLE_S,
    PROBE_INTERVAL_S,
    TICK_STALE_S,
    ProbeStreamingOff,
    Upstream,
)


class FakeIngestor:
    """Records what Upstream asks of it; never touches a socket."""
    def __init__(self):
        self.calls = []
        self.running = False
        self.stop_timeouts = []       # every timeout stop() was called with, in order

    def start(self):
        self.calls.append("start")
        self.running = True

    def stop(self, timeout=None):
        self.calls.append("stop")
        self.stop_timeouts.append(timeout)
        self.running = False
        return True


def _settings(**kw):
    base = dict(on_demand=True, window_max_s=60.0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _up(**kw):
    ing, buf = FakeIngestor(), RingBuffer(1 << 20)
    clk = kw.pop("clock", None)
    if clk is None:
        clk = [1000.0]
        clk_fn = lambda: clk[0]
    else:
        clk_fn = clk
    up = Upstream(ing, buf, _settings(), probe=lambda: None, clock=clk_fn, **kw)
    return up, ing, buf, clk if isinstance(clk, list) else None


def test_a_hold_opens_the_session_and_releasing_closes_it():
    up, ing, _, _ = _up()
    h = up.hold("capture")
    up.tick(now=1000.0)
    assert ing.calls == ["start"]
    h.release()
    up.tick(now=1001.0)
    assert ing.calls == ["start", "stop"]


def test_releasing_twice_cannot_cancel_another_holder():
    up, ing, _, _ = _up()
    a, b = up.hold("viewer"), up.hold("viewer")
    up.tick(now=1000.0)
    a.release()
    a.release()          # idempotent: must not decrement b's count
    up.tick(now=1001.0)
    assert ing.calls == ["start"]
    b.release()
    up.tick(now=1002.0)
    assert ing.calls == ["start", "stop"]


def test_a_viewer_hold_lingers_and_a_capture_hold_does_not():
    up, ing, _, clk = _up()
    with up.hold("viewer", linger_s=30.0):
        up.tick()
    clk[0] = 1020.0
    up.tick()
    assert ing.calls == ["start"]              # still inside the linger
    clk[0] = 1031.0
    up.tick()
    assert ing.calls == ["start", "stop"]


def test_the_hold_is_released_when_the_body_raises():
    up, ing, _, _ = _up()
    with pytest.raises(ValueError):
        with up.hold("capture"):
            up.tick(now=1000.0)
            raise ValueError("decode failed")
    up.tick(now=1001.0)
    assert ing.calls == ["start", "stop"]


def test_on_demand_off_pins_the_connection_open():
    ing, buf = FakeIngestor(), RingBuffer(1 << 20)
    clk = [1000.0]
    up = Upstream(ing, buf, _settings(on_demand=False), probe=lambda: None, clock=lambda: clk[0])
    up.tick()
    assert ing.calls == ["start"]
    clk[0] = 2000.0
    up.tick()
    assert ing.calls == ["start"]              # nothing to release, stays open


def test_holders_names_why_the_phone_is_connected():
    up, _, _, _ = _up()
    assert up.holders() == ()
    h = up.hold("capture")
    assert up.holders() == ("capture",)
    h.release()
    assert up.holders() == ()


def test_holders_names_a_lingering_viewer_while_the_connection_stays_open():
    up, _, _, clk = _up()
    with up.hold("viewer", linger_s=30.0):
        assert up.holders() == ("viewer",)
        up.tick()
    assert up.holders() == ("viewer (linger)",)
    h = up.hold("capture")
    try:
        assert up.holders() == ("capture", "viewer (linger)")
    finally:
        h.release()
    clk[0] = 1031.0
    assert up.holders() == ()


def test_a_stop_that_times_out_is_retried_before_ingest_is_started_again(caplog):
    """Ingestor.stop() returns False when its thread outlived the join. Starting
    a new session then would put two threads on the phone, which wedges the app."""
    up, ing, _, _ = _up()
    results = [False]
    record_stop = ing.stop

    def stop(timeout=None):
        record_stop(timeout)
        return results.pop(0) if results else True

    ing.stop = stop
    h = up.hold("capture")
    up.tick(now=1000.0)
    h.release()
    with caplog.at_level(logging.WARNING, logger="ios2ha_camera.upstream"):
        up.tick(now=1001.0)                    # this stop times out
    h = up.hold("capture")                     # and the next capture wants the phone at once
    try:
        up.tick(now=1002.0)
        up.tick(now=1003.0)
    finally:
        h.release()
    assert ing.calls == ["start", "stop", "stop", "start"]
    assert any(r.levelno == logging.WARNING for r in caplog.records)


def test_a_hold_released_long_after_the_last_tick_gets_full_linger():
    """Linger window should be relative to when hold is released, not last tick."""
    up, ing, _, clk = _up()
    h = up.hold("viewer", linger_s=30.0)
    up.tick()  # clk[0] is 1000.0, connection opens
    assert ing.calls == ["start"]

    # Release hold, but advance clock well past the last tick
    clk[0] = 1500.0
    h.release()  # linger should expire at 1500.0 + 30.0 = 1530.0

    # At 1510, we're past the last tick time but still in the linger window
    clk[0] = 1510.0
    up.tick()
    assert ing.calls == ["start"]  # connection still open

    # At 1531, we're past the linger window
    clk[0] = 1531.0
    up.tick()
    assert ing.calls == ["start", "stop"]  # connection closes


def _probe_returning(buf, ts_box):
    def probe():
        return Frame(b"jpeg", ts_box[0])
    return probe


def test_a_probe_runs_only_while_idle_and_its_frame_lands_in_the_buffer():
    ing, buf = FakeIngestor(), RingBuffer(1 << 20)
    ts = [1000.0]
    up = Upstream(ing, buf, _settings(), probe=_probe_returning(buf, ts))
    up.tick(now=1000.0)                       # idle: probes at once
    assert buf.newest_ts() == 1000.0
    h = up.hold("viewer")
    ts[0] = 1000.0 + PROBE_INTERVAL_S
    up.tick(now=ts[0])                        # session open: no probe
    assert buf.newest_ts() == 1000.0
    h.release()


def test_state_is_idle_after_a_good_probe_and_streaming_once_frames_flow():
    ing, buf = FakeIngestor(), RingBuffer(1 << 20)
    ts = [1000.0]
    up = Upstream(ing, buf, _settings(), probe=_probe_returning(buf, ts))
    up.tick(now=1000.0)
    assert up.state(now=1001.0) == "idle"
    assert up.camera_online(now=1001.0) is True
    with up.hold("viewer"):
        up.tick(now=1002.0)
        buf.append(Frame(b"jpeg", 1002.5))
        assert up.state(now=1003.0) == "streaming"


def test_a_failing_session_is_not_streaming_on_the_strength_of_a_probe_frame():
    """The idle probe's frame is seconds old when a session opens. Counting it
    would call a session that has only ever failed "streaming", and a dead app
    online for as long as that frame stays fresh."""
    up, _, buf, _ = _up()
    buf.append(Frame(b"probe", 990.0))
    h = up.hold("viewer")
    try:
        up.tick(now=1000.0)
        up.on_ingest_state("unreachable")
        assert up.state(now=1005.0) == "unreachable"
    finally:
        h.release()


def test_a_503_probe_reports_streaming_off_and_backs_off():
    ing, buf = FakeIngestor(), RingBuffer(1 << 20)

    def probe():
        raise ProbeStreamingOff()

    up = Upstream(ing, buf, _settings(), probe=probe)
    up.tick(now=1000.0)
    assert up.state(now=1000.0) == "streaming_off"
    assert up.camera_online(now=1000.0) is False
    up.tick(now=1000.0 + PROBE_INTERVAL_S + 1)          # second failure
    up.tick(now=1000.0 + PROBE_INTERVAL_S + 2)          # too soon: backed off
    assert up._probe_failures == 2


def test_camera_online_goes_false_when_the_thread_stops_ticking():
    ing, buf = FakeIngestor(), RingBuffer(1 << 20)
    up = Upstream(ing, buf, _settings(), probe=lambda: Frame(b"jpeg", 1000.0))
    up.tick(now=1000.0)
    assert up.camera_online(now=1000.0 + ONLINE_IDLE_S - 1) is True
    stale = 1000.0 + TICK_STALE_S + 1
    assert up.tick_age(now=stale) > TICK_STALE_S
    assert up.camera_online(now=stale) is False


def test_state_changes_are_published_once_each():
    ing, buf = FakeIngestor(), RingBuffer(1 << 20)
    seen = []
    up = Upstream(ing, buf, _settings(), on_state=seen.append,
                  probe=lambda: Frame(b"jpeg", 1000.0))
    up.tick(now=1000.0)
    up.tick(now=1001.0)
    assert seen == ["idle"]


def test_state_changes_are_logged_exactly_once_each(caplog):
    ing, buf = FakeIngestor(), RingBuffer(1 << 20)
    up = Upstream(ing, buf, _settings(), probe=lambda: Frame(b"jpeg", 1000.0))
    with caplog.at_level(logging.INFO, logger="ios2ha_camera.upstream"):
        up.tick(now=1000.0)
        assert [r.getMessage() for r in caplog.records if r.name == "ios2ha_camera.upstream"] == \
            ["stream_state None -> idle"]
        up.tick(now=1001.0)          # no state change: must not log again
        assert [r.getMessage() for r in caplog.records if r.name == "ios2ha_camera.upstream"] == \
            ["stream_state None -> idle"]


def test_duty_1h_counts_only_time_with_the_connection_open():
    up, _, _, _ = _up()
    h = up.hold("capture")
    up.tick(now=0.0)
    h.release()
    up.tick(now=360.0)                       # open for 360 s of the hour
    assert 9.0 < up.duty_1h(now=3600.0) < 11.0


def _wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_wait_for_frames_counts_only_frames_from_this_session():
    up, _, buf, _ = _up()
    buf.append(Frame(b"old", 100.0))          # a probe frame from before the session
    buf.append(Frame(b"new", 200.0))
    assert up.wait_for_frames(2, since_ts=150.0, timeout=0.05) == 1
    buf.append(Frame(b"new", 201.0))
    assert up.wait_for_frames(2, since_ts=150.0, timeout=0.05) == 2


def test_a_raising_tick_does_not_kill_the_thread():
    up, ing, _, _ = _up()
    boom = [2]

    def tick(now=None):
        if boom[0] > 0:
            boom[0] -= 1
            raise RuntimeError("bang")
        boom[0] -= 1

    up.tick = tick
    up.start()
    try:
        assert _wait_for(lambda: boom[0] < 0)
    finally:
        up.stop()


def test_stop_does_not_touch_the_ingestor_while_the_thread_is_still_ticking():
    """A tick can legitimately block for a while -- a probe waiting out its
    connect/read timeout -- with self._open left True from before the block.
    If join() times out with the thread still stuck there, stop() must not
    also act on self.ing itself, or the calling thread and the still-running
    upstream thread can both drive one connection at once, which is exactly
    what this module exists to prevent."""
    up, ing, _, _ = _up()
    stuck = threading.Event()
    released = threading.Event()

    def tick(now=None):
        up._open = True     # as a real tick would leave it mid-session
        stuck.set()
        released.wait()

    up.tick = tick
    up.start()
    try:
        assert _wait_for(lambda: stuck.is_set())
        up.stop(timeout=0.2)
        assert ing.calls == []      # must not touch ing while the thread is still stuck in a tick
    finally:
        released.set()
        assert _wait_for(lambda: not up._thread.is_alive())


def test_stop_bounds_the_ingestors_own_stop_call_to_the_remaining_budget():
    """stop(timeout) must cover the *whole* call, including the synchronous
    self.ing.stop() that follows a successful thread join -- not just the
    thread join itself. Otherwise a caller who asks for an 8s shutdown can
    still block for 8s (join) + Ingestor's own ~12s default on top."""
    up, ing, _, _ = _up()
    up._open = True     # as tick() would leave it mid-session; no thread running
    up.stop(timeout=3.0)
    assert ing.stop_timeouts, "ing.stop() was never called"
    assert ing.stop_timeouts[-1] is not None
    assert ing.stop_timeouts[-1] <= 3.0


def test_against_the_fake_camera_it_opens_lingers_and_probes():
    buf = RingBuffer(1 << 22)
    with FakeCamera() as cam:
        u = urlparse(cam.url)
        s = _settings(phone_host=u.hostname, phone_port=u.port,
                      phone_user=CREDENTIALS[0], phone_password=CREDENTIALS[1])
        ing = Ingestor(u.hostname, u.port, CREDENTIALS[0], CREDENTIALS[1], buf,
                       backoff_initial=0.05, backoff_max=0.2, read_timeout=2.0)
        up = Upstream(ing, buf, s)
        ing.on_state = up.on_ingest_state
        up.start()
        try:
            assert _wait_for(lambda: buf.newest_ts() is not None)   # the probe alone fills a frame
            assert up.state() == "idle"
            with up.hold("viewer", linger_s=0.2):
                assert _wait_for(lambda: up.state() == "streaming", timeout=5.0)
            assert _wait_for(lambda: up.state() == "idle", timeout=5.0)
        finally:
            up.stop()
