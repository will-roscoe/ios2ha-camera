import io
import threading
import time
import types
import numpy as np
import pytest
from PIL import Image
from ios2ha_camera import live_stack
from ios2ha_camera.live_stack import REFRESH_S, SCALE, THREAD_JOIN_TIMEOUT_S, LiveStacker
from ios2ha_camera.ring_buffer import Frame, RingBuffer


def _settings(**kw):
    base = dict(stacking_mode="auto", stacking_allowed=True, fixed_depth=30, target_level=45.0,
                subject_percentile=10.0, clean_factor=4.0, max_depth=600, window_max_s=60.0,
                ramp_tau_s=900.0, interval_s=600, night_key=0.09, black_level_override=None,
                white_balance="as-shot", live_max_window_s=3.0)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _frame(value, ts, size=(1920, 1080)):
    arr = np.full((size[1], size[0], 3), value, dtype=np.uint8)
    b = io.BytesIO()
    Image.fromarray(arr).save(b, "JPEG", quality=92)
    return Frame(b.getvalue(), ts)


def _coloured_highlight_frame(ts, size=(1920, 1080)):
    """A coloured gradient (each channel its own slope, so hue varies across
    most of the frame and the desaturation blend has something to undo) with
    a small, strongly saturated highlight patch.

    The patch is under 0.1% of the pixels -- smaller than the 99.9th
    percentile `refresh_params` uses to estimate the white point -- so it is
    excluded from that estimate as an outlier and its scaled, pre-gamma value
    overshoots 1.0 by a wide margin (measured ~1.7, not the ~1.0 a smooth
    ramp gives), the way an accidental reflection or a light in frame would.
    render.render() sees the same excluded outlier and clips it the same way,
    so this only distinguishes the two paths when one of them stops clipping.
    """
    w, h = size
    arr = np.empty((h, w, 3), dtype=np.float64)
    arr[..., 0] = np.linspace(5, 180, w)
    arr[..., 1] = np.linspace(15, 140, w)
    arr[..., 2] = np.linspace(30, 90, w)
    arr[500:540, 900:940] = (255, 8, 8)                # ~0.08% of the frame
    b = io.BytesIO()
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(b, "JPEG", quality=92)
    return Frame(b.getvalue(), ts)


def test_output_is_half_resolution():
    st = LiveStacker(RingBuffer(1 << 22), _settings())
    jpeg = st.step(_frame(40, 1000.0), now=1000.0)
    im = Image.open(io.BytesIO(jpeg))
    assert (im.width, im.height) == (1920 // SCALE, 1080 // SCALE)


def test_the_warm_up_is_the_running_mean_of_what_it_has_seen():
    """A running mean for the first N frames: frame 1 alone, then the mean of
    2, then of 3 -- each exact as long as the chosen depth N is at least the
    frame count so far, so a = max(1/t, 1/N) is just 1/t (measured N == 3 for
    this dark, uniform frame -- exactly enough for the three frames here)."""
    st = LiveStacker(RingBuffer(1 << 22), _settings())
    st.step(_frame(40, 1000.0), now=1000.0)
    assert st.accumulated().mean() == 40.0
    st.step(_frame(80, 1000.1), now=1000.1)
    assert abs(st.accumulated().mean() - 60.0) < 0.5
    st.step(_frame(120, 1000.2), now=1000.2)
    assert abs(st.accumulated().mean() - 80.0) < 0.5


def test_a_gap_resets_the_accumulator():
    st = LiveStacker(RingBuffer(1 << 22), _settings())
    st.step(_frame(40, 1000.0), now=1000.0)
    st.step(_frame(200, 1010.0), now=1010.0)          # 10 s later: a new session
    assert abs(st.accumulated().mean() - 200.0) < 0.5


def test_the_live_cap_bounds_the_depth():
    st = LiveStacker(RingBuffer(1 << 22), _settings(live_max_window_s=1.0))
    st.step(_frame(2, 1000.0), now=1000.0)            # very dark: the depth law wants hundreds
    st.refresh_params(fps=13.9)
    assert st.depth() <= 14


def test_the_tone_path_matches_render_within_the_measured_tolerance():
    from ios2ha_camera import render
    s = _settings()
    st = LiveStacker(RingBuffer(1 << 22), s)
    st.step(_coloured_highlight_frame(1000.0), now=1000.0)
    acc = st.accumulated()
    bare = types.SimpleNamespace(**{**vars(s), "denoise_strength": 0.0, "chroma_radius": 0.0,
                                    "render_intent": "natural", "stretch_strength": 25.0})
    ref = render.render(acc.astype(np.float64), bare)
    mine = st.tone(acc)
    assert np.abs(mine.astype(np.int16) - ref.astype(np.int16)).max() <= 3
    # A uniform or colourless frame would make both paths output flat black,
    # or would make the desaturation blend a no-op, regardless of whether the
    # tone curve ran correctly; require actual spread in the output so this
    # test cannot pass vacuously.
    assert np.percentile(mine, 99) - np.percentile(mine, 1) >= 50


class _SlowBuffer:
    """A buffer whose latest() blocks for a bit, standing in for real I/O so
    the outgoing thread cannot notice a stop request until that call returns
    -- the only way to prove from outside that attach() itself no longer
    waits for it."""

    def __init__(self, delay):
        self._delay = delay

    def latest(self):
        time.sleep(self._delay)
        return None


def test_attach_after_a_quick_detach_does_not_leave_the_old_thread_running():
    """A detach() immediately followed by attach() -- plausible on a viewer
    reconnect -- used to start a second _run thread without checking whether
    the first had actually exited: both would then clear and poll the same
    Event and mutate self._acc/_t/_params/_out_times with no lock around
    per-frame work.

    attach() is called from an aiohttp handler and must never block (see
    live.py's docstring), so the join of the outgoing thread moved out of
    attach() and into the new thread itself -- one accumulator, one writer
    still holds, because the new thread will not touch the accumulator until
    that join returns, but attach()'s caller no longer pays for the wait.

    Proven two ways: the old thread is still guaranteed gone soon after (a
    bounded join, not a sleep, since the exact moment is not otherwise
    observable), and the second attach() call itself is timed to show it did
    not perform that wait synchronously -- with a plain blocking join it
    could not return before the slow buffer's delay elapses.
    """
    delay = 0.3
    st = LiveStacker(_SlowBuffer(delay), _settings())
    st.attach()
    first = st._thread
    try:
        st.detach()
        started = time.monotonic()
        st.attach()
        elapsed = time.monotonic() - started
        second = st._thread
        assert second is not first
        # The old, blocking attach() had to wait out the slow buffer's delay
        # (via the outgoing thread's own poll) before returning; the
        # non-blocking one returns long before that delay has even elapsed.
        assert elapsed < delay / 2
        first.join(THREAD_JOIN_TIMEOUT_S)
        assert not first.is_alive()
    finally:
        st.detach()


def test_tone_parameters_are_reestimated_on_the_refresh_interval_not_every_frame():
    st = LiveStacker(RingBuffer(1 << 22), _settings())
    refreshes = []
    real = st.refresh_params
    st.refresh_params = lambda **kw: (refreshes.append(kw), real(**kw))[1]
    st.step(_frame(40, 1000.0), now=1000.0)
    st.step(_frame(40, 1001.0), now=1001.0)
    assert len(refreshes) == 1
    st.step(_frame(40, 1002.0), now=1000.0 + REFRESH_S + 0.1)
    assert len(refreshes) == 2


def _wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _join(*threads):
    for t in threads:
        if t is not None and t.ident is not None:
            t.join(5.0)


def test_a_thread_that_fails_to_start_does_not_disable_the_feed(monkeypatch):
    """live.py skips detach() when attach() raises, so a viewer count left
    behind by a failed start stopped every later attach() from starting a
    thread, and /live/stacked served nothing for the life of the process."""
    real_start = threading.Thread.start
    failed = []

    def start_failing_once(thread):
        if thread.name == "live-stack" and not failed:
            failed.append(thread)
            raise RuntimeError("can't start new thread")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", start_failing_once)
    st = LiveStacker(RingBuffer(1 << 20), _settings())
    try:
        with pytest.raises(RuntimeError):
            st.attach()
        st.attach()
        assert st._thread is not None and st._thread.is_alive()
    finally:
        st.detach()
        _join(st._thread)


class _GatedBuffer:
    """Holds the first thread to read it until released and serves every other
    thread one unchanging frame, so the test, not timing, decides when each
    generation moves."""

    def __init__(self, frame):
        self.frame = frame
        self.held = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def latest(self):
        with self._lock:
            if self.held is None:
                self.held = threading.current_thread()
        if threading.current_thread() is self.held:
            self.entered.set()
            self.release.wait(5.0)
            return None
        return self.frame


def test_an_outgoing_thread_that_outlives_the_join_leaves_its_successor_alone(monkeypatch):
    """_run_after gives up on its predecessor after THREAD_JOIN_TIMEOUT_S, so
    the predecessor can finish after its successor has started. Its teardown
    must not then blank the successor's accumulator and latest frame."""
    monkeypatch.setattr(live_stack, "THREAD_JOIN_TIMEOUT_S", 0.05)
    buf = _GatedBuffer(_frame(40, time.time()))
    st = LiveStacker(buf, _settings())
    first = second = None
    try:
        st.attach()
        first = st._thread
        assert buf.entered.wait(5.0)
        st.detach()
        st.attach()
        second = st._thread
        assert _wait_for(lambda: st.latest() is not None)
        buf.release.set()
        first.join(5.0)
        assert not first.is_alive()
        assert st.accumulated() is not None
        assert st.latest() is not None
    finally:
        buf.release.set()
        st.detach()
        _join(first, second)
