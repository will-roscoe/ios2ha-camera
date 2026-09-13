import numpy as np
import pytest

from ios2ha_camera import render
from ios2ha_camera.settings import Settings


def _dark_scene():
    a = np.full((32, 32, 3), 3.0, dtype=np.float64)
    a[8:24, 8:24] = [6.0, 5.0, 9.0]   # a slightly brighter patch
    return a


def _bright_noisy_scene():
    # Well above target_level (45.0 by default): the camera's own exposure
    # already meets it, so this stands in for a real daylight frame.
    rng = np.random.default_rng(0)
    return np.clip(200.0 + rng.normal(0, 15, (64, 64, 3)), 0, 255)


def test_natural_brighter_than_input_but_not_maxed():
    s = Settings(); s.render_intent = "natural"
    out = render.render(_dark_scene(), s)
    assert out.dtype == np.uint8
    assert out.mean() > 20 and out.mean() < 200


def test_inspection_uses_more_range_than_natural():
    s = Settings()
    s.render_intent = "natural"; nat = render.render(_dark_scene(), s).mean()
    s.render_intent = "inspection"; insp = render.render(_dark_scene(), s).mean()
    assert insp > nat


def test_as_shot_hue_preserved_on_grey():
    s = Settings(); s.white_balance = "as-shot"
    grey = np.full((16, 16, 3), 20.0)
    out = render.render(grey, s).astype(float)
    assert abs(out[..., 0].mean() - out[..., 2].mean()) < 8


def test_to_jpeg_roundtrips():
    b = render.to_jpeg(np.zeros((16, 16, 3), np.uint8))
    assert b[:3] == b"\xff\xd8\xff"


def test_tone_map_uses_one_shared_scale_not_a_per_channel_curve():
    # Two regions with the *same* R:G:B ratio (3:2:1) at different absolute
    # brightness. A tone curve that derives one factor from luminance and
    # applies it to all three channels reproduces close to the same output
    # ratio in both regions -- the shared factor cancels out of the ratio
    # regardless of how bright the region is. A curve looked up per channel
    # (treating each channel as if it were its own luminance) compresses the
    # bright region's channels by different amounts than the dim region's,
    # so the ratio drifts noticeably between them. This is the property that
    # keeps hue intact through tone mapping; it must hold for the real
    # render() path, not just a hand-picked internal helper.
    a = np.full((40, 40, 3), 3.0, dtype=np.float64)
    a[0:4, 0:4] = 400.0                    # bright reference corner, well outside both regions
    a[5:15, 5:15] = [9.0, 6.0, 3.0]        # region A: ratio 3:2:1, dim
    a[25:35, 25:35] = [15.0, 10.0, 5.0]    # region B: same ratio 3:2:1, brighter

    s = Settings()
    s.render_intent = "inspection"
    s.white_balance = "as-shot"
    s.black_level_override = (0.0, 0.0, 0.0)
    s.chroma_radius = 0
    s.denoise_strength = 0

    out = render.render(a, s).astype(float)
    A = out[5:15, 5:15].reshape(-1, 3).mean(axis=0)
    B = out[25:35, 25:35].reshape(-1, 3).mean(axis=0)
    ratio_a = A[0] / A[2]
    ratio_b = B[0] / B[2]
    assert abs(ratio_a - ratio_b) < 0.15


# -- night_weight ------------------------------------------------------------

def test_night_weight_is_zero_once_the_subject_meets_target():
    assert render.night_weight(45.0, 45.0) == 0.0
    assert render.night_weight(60.0, 45.0) == 0.0   # brighter still: no brightening needed either


def test_night_weight_is_zero_exactly_at_gain_one():
    # G = target / subject = 1
    assert render.night_weight(45.0, 45.0) == 0.0


def test_night_weight_is_one_third_at_gain_two():
    assert render.night_weight(22.5, 45.0) == pytest.approx(1 / 3)


def test_night_weight_is_one_at_and_beyond_gain_four():
    assert render.night_weight(11.25, 45.0) == 1.0    # G == NIGHT_GAIN == 4
    assert render.night_weight(5.0, 45.0) == 1.0       # G > NIGHT_GAIN


def test_night_weight_is_monotonic_in_between():
    subjects = [45.0, 30.0, 22.5, 15.0, 11.25, 5.0]    # decreasing subject -> increasing gain
    weights = [render.night_weight(s, 45.0) for s in subjects]
    assert weights == sorted(weights)


def test_night_weight_is_safe_at_a_subject_of_zero_or_below():
    assert render.night_weight(0.0, 45.0) == 1.0
    assert render.night_weight(-5.0, 45.0) == 1.0


def test_night_weight_is_safe_at_a_nan_subject():
    # NaN <= 0 is False, so a naive non-positive guard lets it through; every
    # comparison with NaN is false, which is exactly what "not subject > 0"
    # exploits to catch it too.
    assert render.night_weight(float("nan"), 45.0) == 1.0


def test_render_with_a_nan_subject_gives_the_normal_night_render_not_black():
    """A corrupted subject measurement must fail towards the safe (night)
    render, not silently publish a black still."""
    s = Settings(); s.render_intent = "natural"
    scene = _dark_scene()
    weight = render.night_weight(float("nan"), s.target_level)
    out = render.render(scene, s, night_weight=weight)
    full = render.render(scene, s, night_weight=1.0)
    assert np.array_equal(out, full)
    assert out.mean() > 5, "a NaN subject must not silently render an all-black frame"


# -- render()'s day/night blend -----------------------------------------------

def test_zero_weight_keeps_the_camera_look_and_skips_denoise(monkeypatch):
    import cv2
    calls = []
    real_denoise = cv2.fastNlMeansDenoisingColored

    def spy(*a, **k):
        calls.append(1)
        return real_denoise(*a, **k)

    monkeypatch.setattr(cv2, "fastNlMeansDenoisingColored", spy)

    s = Settings()
    scene = _bright_noisy_scene()
    out0 = render.render(scene, s, night_weight=0.0)
    as_shot = np.clip(scene, 0, 255).astype(np.uint8)
    assert np.abs(out0.astype(int) - as_shot.astype(int)).max() <= 2
    assert calls == [], "denoise ran on a zero-weight (daylight) render"

    render.render(scene, s, night_weight=1.0)
    assert calls, "denoise never ran at all -- the spy itself is broken"


def test_default_night_weight_matches_the_full_night_render_exactly():
    s = Settings(); s.render_intent = "natural"
    scene = _dark_scene()
    default_out = render.render(scene, s)
    full_out = render.render(scene, s, night_weight=1.0)
    assert np.array_equal(default_out, full_out)
    as_shot = np.clip(scene, 0, 255).astype(np.uint8)
    assert not np.array_equal(default_out, as_shot)


def test_inspection_ignores_the_blend_entirely():
    s = Settings(); s.render_intent = "inspection"
    scene = _dark_scene()
    out_a = render.render(scene, s, night_weight=0.0)
    out_b = render.render(scene, s, night_weight=1.0)
    out_c = render.render(scene, s, night_weight=0.37)
    assert np.array_equal(out_a, out_b)
    assert np.array_equal(out_b, out_c)
