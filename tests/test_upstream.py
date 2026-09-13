import types
import pytest
from ios2ha_camera.ring_buffer import Frame, RingBuffer
from ios2ha_camera.upstream import Upstream


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
