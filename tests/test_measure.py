import numpy as np

from ios2ha_camera import measure


def _img(val): return np.full((32, 32, 3), val, dtype=np.float32)


def test_black_level_is_low_percentile():
    a = _img(10.0); a[0, 0] = 0.0
    bl = measure.black_level(a)
    assert bl.shape == (3,) and (bl <= 10.0).all()


def test_required_depth_grows_quadratically():
    d1 = measure.required_depth(subject=10.0, target=40.0, k=4.0)   # gain 4
    d2 = measure.required_depth(subject=5.0, target=40.0, k=4.0)    # gain 8
    assert d2 >= 3.5 * d1                                           # ~4x


def test_required_depth_floor_when_bright():
    # Bright scenes still stack ceil(k) frames per spec: G = max(T/S, 1)
    assert measure.required_depth(subject=200.0, target=40.0, k=4.0) == 4


def test_motion_zero_for_identical():
    a = _img(30.0)
    assert measure.motion_level(a, a) == 0.0


def test_colour_temperature_reasonable_for_warm():
    warm = np.zeros((16, 16, 3), np.float32)
    warm[..., 0] = 200; warm[..., 1] = 150; warm[..., 2] = 90
    assert 1500 < measure.colour_temperature_k(warm) < 6500


def test_luminance_percentiles_default_includes_extremes():
    a = _img(100.0)
    black = np.array([10.0, 10.0, 10.0])
    pcts = measure.luminance_percentiles(a, black)
    assert 0.5 in pcts and 99.5 in pcts
    assert pcts[0.5] <= pcts[99.5]
    assert all(p in pcts for p in (0.5, 1, 10, 50, 90, 99, 99.5))


def test_required_depth_handles_total_darkness():
    d = measure.required_depth(subject=0.0, target=45.0, k=4.0)
    assert isinstance(d, int) and d > 0 and np.isfinite(d)


def test_focus_sharpness_flat_image_near_zero():
    flat = _img(128.0)
    sharpness = measure.focus_sharpness(flat)
    assert sharpness < 1.0


def test_focus_sharpness_edges_score_higher():
    edged = _img(128.0)
    # Add hard horizontal edge in the middle
    edged[16:, :] = 64.0
    sharpness_edged = measure.focus_sharpness(edged)

    flat = _img(128.0)
    sharpness_flat = measure.focus_sharpness(flat)

    assert sharpness_edged > sharpness_flat


def test_focus_sharpness_blur_reduces_score():
    import cv2
    edged = _img(128.0)
    edged[16:, :] = 64.0
    sharpness_sharp = measure.focus_sharpness(edged)

    blurred = cv2.GaussianBlur(edged, (5, 5), 1.0)
    sharpness_blurred = measure.focus_sharpness(blurred)

    assert sharpness_blurred < sharpness_sharp
