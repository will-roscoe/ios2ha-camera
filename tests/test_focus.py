"""Far-field autofocus: scoring and the search.

Measured 2026-09-28 on a night skyline through a window: night autofocus parked the
lens at 0.68 while edge detail peaked at 0.78-0.82, about five times higher, and by
23:00 the peak had drifted to 0.77. Single night frames were too dark to judge, so
a probe averages ~35 frames. The window and a curtain sit within a metre and focus
below 0.60, so the search never looks there; between peaks it cannot tell apart,
the far one wins.
"""
import cv2
import numpy as np
import pytest

from ios2ha_camera import focus


def _scene(h=270, w=480, seed=0):
    """Half-size luma of a view with fine structure: windows, edges, texture."""
    rng = np.random.default_rng(seed)
    img = np.full((h, w), 4.0, np.float32)
    for i in range(6):
        x0 = 60 + i * 60
        img[120:, x0:x0 + 45] = 10 + 3 * i
        for wy in range(135, 260, 14):
            for wx in range(x0 + 4, x0 + 40, 9):
                if rng.random() < 0.5:
                    img[wy:wy + 6, wx:wx + 5] = 40.0
    return img + rng.normal(0, 0.3, img.shape).astype(np.float32)


def _blurred(img, sigma):
    return cv2.GaussianBlur(img, (0, 0), sigma) if sigma > 0 else img


# -- the score -----------------------------------------------------------------

def test_a_sharper_picture_scores_higher():
    img = _scene()
    assert focus.score(img) > focus.score(_blurred(img, 1.5)) > focus.score(_blurred(img, 3.0))


def test_detail_in_the_border_is_ignored():
    """The curtain and the window frame sit at the edges of the view."""
    flat = np.full((270, 480), 5.0, np.float32)
    edged = flat.copy()
    edged[:, :20] = np.random.default_rng(1).uniform(0, 60, (270, 20))
    assert focus.score(edged) == pytest.approx(focus.score(flat), abs=1e-6)


def test_a_small_detailed_area_is_not_diluted_by_empty_sky():
    """The score is the most detailed quarter of the picture, so a skyline across
    the bottom third counts as much as if it filled the frame."""
    img = _scene()
    sky_only = img.copy()
    sky_only[120:] = 4.0
    assert focus.score(img) > 10 * focus.score(sky_only)


# -- the search ----------------------------------------------------------------

def _curve(peaks, noise=0.0, seed=0):
    """A sharpness curve: a sum of bumps, each (position, height, width)."""
    rng = np.random.default_rng(seed)
    probed = []

    def measure(p):
        probed.append(p)
        v = sum(h * np.exp(-((p - c) / w) ** 2) for c, h, w in peaks)
        return float(v * (1 + noise * rng.normal()) + 0.05)
    return measure, probed


def test_a_single_peak_is_found():
    measure, probed = _curve([(0.78, 5.0, 0.06)])
    position, curve = focus.search(measure)
    assert position == pytest.approx(0.78, abs=0.01)
    assert len(probed) <= 14
    assert all(focus.NEAR <= p <= focus.FAR for p in probed)


def test_the_far_peak_wins_when_two_are_equal():
    measure, _ = _curve([(0.66, 5.0, 0.03), (0.80, 5.0, 0.03)])
    position, _ = focus.search(measure)
    assert position == pytest.approx(0.80, abs=0.015)


def test_a_near_peak_out_of_range_is_never_looked_at():
    """Window glass, a curtain: much sharper, much nearer, never probed."""
    measure, probed = _curve([(0.35, 50.0, 0.05), (0.79, 3.0, 0.06)])
    position, _ = focus.search(measure)
    assert position == pytest.approx(0.79, abs=0.015)
    assert min(probed) >= focus.NEAR


def test_a_noisy_curve_still_lands_near_the_peak():
    measure, _ = _curve([(0.77, 5.0, 0.07)], noise=0.03, seed=3)
    position, _ = focus.search(measure)
    assert position == pytest.approx(0.77, abs=0.02)


@pytest.mark.parametrize("peak", [1.0, 0.6])
def test_a_peak_at_either_end_of_the_range(peak):
    measure, _ = _curve([(peak, 5.0, 0.08)])
    position, _ = focus.search(measure)
    assert position == pytest.approx(peak, abs=0.015)


def test_no_clear_peak_changes_nothing():
    """Fog, darkness, a covered lens: every position looks the same."""
    measure, _ = _curve([(0.78, 0.02, 0.5)])
    with pytest.raises(focus.NoClearPeak) as err:
        focus.search(measure)
    assert len(err.value.curve) == len(focus.COARSE)


def test_the_curve_records_every_probe():
    measure, probed = _curve([(0.78, 5.0, 0.06)])
    _, curve = focus.search(measure)
    assert sorted(curve) == sorted(set(round(p, 4) for p in probed))
