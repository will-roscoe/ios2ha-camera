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
