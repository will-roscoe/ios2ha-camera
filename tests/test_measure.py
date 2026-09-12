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
    assert measure.required_depth(subject=200.0, target=40.0, k=4.0) == 1


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
