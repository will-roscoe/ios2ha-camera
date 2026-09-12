import numpy as np
from ios2ha_camera import render
from ios2ha_camera.settings import Settings


def _dark_scene():
    a = np.full((32, 32, 3), 3.0, dtype=np.float64)
    a[8:24, 8:24] = [6.0, 5.0, 9.0]   # a slightly brighter patch
    return a


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
