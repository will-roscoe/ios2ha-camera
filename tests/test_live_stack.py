import io
import types
import numpy as np
from PIL import Image
from ios2ha_camera.live_stack import SCALE, LiveStacker
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


def _gradient_frame(ts, size=(1920, 1080)):
    """A horizontal ramp, identical in all three channels: unlike a uniform
    frame, this has an actual black level distinct from its highlights, so the
    tone curve has something to do."""
    w, h = size
    row = np.linspace(5, 200, w).astype(np.uint8)
    arr = np.broadcast_to(row, (h, w)).copy()
    arr = np.stack([arr, arr, arr], axis=-1)
    b = io.BytesIO()
    Image.fromarray(arr).save(b, "JPEG", quality=92)
    return Frame(b.getvalue(), ts)


def test_output_is_half_resolution():
    st = LiveStacker(RingBuffer(1 << 22), _settings())
    jpeg = st.step(_frame(40, 1000.0), now=1000.0)
    im = Image.open(io.BytesIO(jpeg))
    assert (im.width, im.height) == (1920 // SCALE, 1080 // SCALE)


def test_the_warm_up_is_the_running_mean_of_what_it_has_seen():
    """A running mean for the first N frames: frame 1 alone, then the mean of
    2, then of 3 -- each exact because the chosen depth (N, in the hundreds
    for this dark, uniform frame) is far larger than the frame count so far,
    so a = max(1/t, 1/N) is just 1/t."""
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
    st.step(_gradient_frame(1000.0), now=1000.0)
    acc = st.accumulated()
    bare = types.SimpleNamespace(**{**vars(s), "denoise_strength": 0.0, "chroma_radius": 0.0,
                                    "render_intent": "natural", "stretch_strength": 25.0})
    ref = render.render(acc.astype(np.float64), bare)
    mine = st.tone(acc)
    assert np.abs(mine.astype(np.int16) - ref.astype(np.int16)).max() <= 3
    # A uniform frame would make both paths output flat black regardless of
    # whether the tone curve ran at all; require actual spread in the output
    # so this test cannot pass vacuously.
    assert np.percentile(mine, 99) - np.percentile(mine, 1) >= 50
