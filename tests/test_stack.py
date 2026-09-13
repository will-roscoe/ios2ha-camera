import io
import tracemalloc

import numpy as np
from PIL import Image

from ios2ha_camera import stack
from ios2ha_camera.ring_buffer import Frame
from ios2ha_camera.settings import Settings


def _frame(val, noise, seed, size=32):
    rng = np.random.default_rng(seed)
    arr = np.clip(val + rng.normal(0, noise, (size, size, 3)), 0, 255).astype("uint8")
    b = io.BytesIO(); Image.fromarray(arr).save(b, "JPEG", quality=95)
    return Frame(b.getvalue(), ts=float(seed))


def test_stacking_reduces_noise():
    frames = [_frame(40, 15, i) for i in range(40)]
    single = stack.decode(frames[0])
    stacked = stack.stack_frames(frames)
    assert stacked.std() < single.std()


def test_choose_depth_clamps_to_max():
    s = Settings(); s.max_depth = 50; s.clean_factor = 4.0; s.target_level = 40.0
    d = stack.choose_depth(subject=1.0, settings=s, fps=12.0, previous_depth=None)
    assert 1 <= d <= 50


def test_choose_depth_window_limit():
    s = Settings(); s.max_depth = 100000; s.window_max_s = 10.0
    d = stack.choose_depth(subject=1.0, settings=s, fps=12.0, previous_depth=None)
    assert d <= int(12.0 * 10.0)


def test_choose_depth_off_mode_is_always_one():
    # stacking_mode "off" must force depth 1 regardless of how dim the subject is.
    s = Settings(); s.stacking_mode = "off"
    d = stack.choose_depth(subject=0.1, settings=s, fps=12.0, previous_depth=50)
    assert d == 1


def test_choose_depth_stacking_disallowed_overrides_auto():
    # stacking_allowed=False exists so Home Assistant can forbid stacking outright,
    # even while stacking_mode is left on "auto".
    s = Settings(); s.stacking_mode = "auto"; s.stacking_allowed = False
    d = stack.choose_depth(subject=0.1, settings=s, fps=12.0, previous_depth=50)
    assert d == 1


def test_choose_depth_fixed_mode_uses_fixed_depth():
    s = Settings(); s.stacking_mode = "fixed"; s.fixed_depth = 20
    s.max_depth = 1000; s.window_max_s = 60.0
    d = stack.choose_depth(subject=1.0, settings=s, fps=12.0, previous_depth=None)
    assert d == 20


def test_choose_depth_ramps_towards_target_not_instantly():
    # With a previous depth far from the new target, the EMA should move only
    # partway there in one interval rather than jumping straight to it.
    s = Settings(); s.stacking_mode = "fixed"; s.fixed_depth = 500
    s.max_depth = 1000; s.window_max_s = 600.0
    s.interval_s = 600; s.ramp_tau_s = 900.0
    d = stack.choose_depth(subject=1.0, settings=s, fps=12.0, previous_depth=1)
    assert 1 < d < 500


def test_sigma_clipped_mean_is_not_median():
    # Six low samples and one high outlier per pixel: the median collapses to
    # the low value (this is exactly the posterising failure mode measured on
    # a dim subject), but the true mean sits well above it. A sigma-clipped
    # mean must track the mean family and land near the mean, not the median.
    low = np.zeros((2, 2, 3), dtype=np.float32)
    high = np.full((2, 2, 3), 70.0, dtype=np.float32)
    arrays = [low] * 6 + [high]
    median_result = np.median(np.stack(arrays), axis=0)
    result = stack.sigma_clipped_mean(arrays)
    assert median_result.mean() == 0.0
    assert result.mean() > 5.0
    assert abs(result.mean() - 10.0) < 2.0


def test_stack_frames_matches_in_memory_sigma_clipped_mean():
    # stack_frames decodes on a bounded thread pool instead of building an
    # arrays list up front, but it must reach the same shared accumulator
    # maths as sigma_clipped_mean on the same underlying frame bytes -- both
    # call decode() on identical Frame.data and sum in the same order, so
    # this should hold to floating-point precision, not just approximately.
    frames = [_frame(60, 20, i) for i in range(9)]
    batch = stack.sigma_clipped_mean([stack.decode(f) for f in frames])
    streamed = stack.stack_frames(frames)
    assert np.allclose(batch, streamed, atol=1e-6)


def test_stack_frames_memory_bounded_in_frame_count():
    # A batch implementation decodes every frame up front, so its peak memory
    # scales with n. Pick n large enough that n * one_frame_bytes is far past
    # any sane small-multiple bound, and confirm the streaming path's actual
    # peak (measured via tracemalloc, which numpy's default allocator reports
    # to) doesn't grow anywhere near that -- it should stay within a handful
    # of decoded frames plus the per-pixel accumulators, independent of n.
    n = 300
    frames = [_frame(60, 20, i, size=48) for i in range(n)]
    one_frame_bytes = stack.decode(frames[0]).nbytes

    tracemalloc.start()
    tracemalloc.clear_traces()
    stack.stack_frames(frames)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # A naive decode-everything-first implementation would need roughly
    # n (300) times one_frame_bytes here; the streaming path measures at
    # ~29x in practice, flat regardless of n -- 60x leaves headroom without
    # coming close to the naive n=300x figure it must be distinguished from.
    assert peak < 60 * one_frame_bytes


def test_hard_cap_overrides_the_settings_caps():
    s = Settings(); s.stacking_mode = "auto"; s.max_depth = 600; s.window_max_s = 60.0; s.interval_s = 600; s.clean_factor = 4.0; s.target_level = 40.0
    assert stack.choose_depth(1.0, s, fps=13.9, previous_depth=None) > 100
    assert stack.choose_depth(1.0, s, fps=13.9, previous_depth=None, hard_cap=41) == 41


def test_without_hard_cap_nothing_changes():
    s = Settings(); s.stacking_mode = "auto"; s.max_depth = 50; s.window_max_s = 60.0; s.interval_s = 600; s.clean_factor = 4.0; s.target_level = 40.0
    assert stack.choose_depth(1.0, s, fps=13.9, previous_depth=None) == 50
