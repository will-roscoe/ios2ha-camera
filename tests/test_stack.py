import io, numpy as np
from PIL import Image
from ios2ha_camera.ring_buffer import Frame
from ios2ha_camera import stack
from ios2ha_camera.settings import Settings


def _frame(val, noise, seed):
    rng = np.random.default_rng(seed)
    arr = np.clip(val + rng.normal(0, noise, (32, 32, 3)), 0, 255).astype("uint8")
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
