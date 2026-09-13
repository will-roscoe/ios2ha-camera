import types
import pytest
from ios2ha_camera.ring_buffer import Frame, RingBuffer
from ios2ha_camera.upstream import Upstream, ONLINE_IDLE_S, PROBE_INTERVAL_S, ProbeStreamingOff, TICK_STALE_S


class FakeIngestor:
    """Records what Upstream asks of it; never touches a socket."""
    def __init__(self):
        self.calls = []
        self.running = False

    def start(self):
        self.calls.append("start")
        self.running = True

    def stop(self, timeout=None):
        self.calls.append("stop")
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


def test_duty_1h_counts_only_time_with_the_connection_open():
    up, _, _, _ = _up()
    h = up.hold("capture")
    up.tick(now=0.0)
    h.release()
    up.tick(now=360.0)                       # open for 360 s of the hour
    assert 9.0 < up.duty_1h(now=3600.0) < 11.0
