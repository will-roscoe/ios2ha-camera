"""Night colour held to a region Will picks.

Measured 2026-09-28: covering the lens, and later a curtain opening behind the
camera, moved the phone's automatic white balance, and at night it cannot come back
-- stills stayed cyan (green/red 1.48 against 0.97). Restarting the app's capture
session fixed it once and made it bluer the second time, and the app exposes no
white-balance control. So a region of the view is remembered from a still that
looks right, and later night stills are pulled back to its colour.
"""
import cv2
import numpy as np
import pytest

from ios2ha_camera.colour_ref import ColourReference

REGION = (0.30, 0.55, 0.45, 0.80)    # x0, y0, x1, y1 as fractions


def _view(h=270, w=480, seed=0):
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.float32)
    img[:] = (1.0, 1.1, 1.3)
    for i, colour in enumerate([(12, 11, 9), (20, 16, 12), (8, 9, 10), (30, 28, 22)]):
        x0 = 30 + i * 110
        img[140:, x0:x0 + 90] = colour
        for wy in range(160, 250, 25):
            for wx in range(x0 + 10, x0 + 80, 20):
                if rng.random() < 0.5:
                    img[wy:wy + 8, wx:wx + 10] = (80, 60, 30)
    return img + rng.normal(0, 0.15, img.shape).astype(np.float32)


def _recorded(tmp_path, img, region=REGION):
    ref = ColourReference(str(tmp_path / "ref.npz"))
    assert ref.record(img, region) is None
    return ref


def _colour(img, region=REGION):
    h, w = img.shape[:2]
    x0, y0, x1, y1 = region
    return np.median(img[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)].reshape(-1, 3), axis=0)


def test_the_region_gets_its_colour_back(tmp_path):
    img = _view()
    ref = _recorded(tmp_path, img)
    now = img * np.float32((0.8, 1.0, 1.35))                   # cyan, as measured
    gains, state = ref.gains(now)
    assert state == "applied"
    want, got = _colour(img), _colour(now * gains)
    assert np.allclose(got / got[1], want / want[1], atol=0.02)


def test_brightness_is_left_alone(tmp_path):
    img = _view()
    ref = _recorded(tmp_path, img)
    now = img * np.float32((0.8, 1.0, 1.35)) * 1.7
    gains, _ = ref.gains(now)
    assert (now * gains).mean() == pytest.approx(now.mean(), rel=0.05)


@pytest.mark.parametrize("dx, dy", [(12, 0), (0, -9), (-15, 7)])
def test_a_small_move_of_the_camera_is_followed(tmp_path, dx, dy):
    img = _view()
    ref = _recorded(tmp_path, img)
    moved = np.roll(np.roll(img, dy, axis=0), dx, axis=1) * np.float32((0.85, 1.0, 1.25))
    gains, state = ref.gains(moved)
    assert state == "applied"
    want = _colour(img)
    h, w = img.shape[:2]
    shifted = (REGION[0] + dx / w, REGION[1] + dy / h, REGION[2] + dx / w, REGION[3] + dy / h)
    got = _colour(moved * gains, shifted)
    assert np.allclose(got / got[1], want / want[1], atol=0.03)


def test_a_different_view_is_not_corrected(tmp_path):
    ref = _recorded(tmp_path, _view(seed=0))
    gains, state = ref.gains(np.flipud(_view(seed=7)).copy())
    assert gains is None and state == "no_match"


def test_nothing_is_done_without_a_reference(tmp_path):
    gains, state = ColourReference(str(tmp_path / "absent.npz")).gains(_view())
    assert gains is None and state == "none"


def test_a_region_too_dark_to_measure_is_refused(tmp_path):
    ref = ColourReference(str(tmp_path / "ref.npz"))
    assert ref.record(np.full((270, 480, 3), 0.3, np.float32), REGION) == "too_dark"
    assert not ref.ready


def test_the_reference_survives_a_restart(tmp_path):
    img = _view()
    _recorded(tmp_path, img)
    again = ColourReference(str(tmp_path / "ref.npz"))
    assert again.ready
    _, state = again.gains(img * np.float32((0.8, 1.0, 1.35)))
    assert state == "applied"


def test_a_full_resolution_still_is_accepted(tmp_path):
    big = np.kron(_view(), np.ones((4, 4, 1), np.float32))
    ref = _recorded(tmp_path, big)
    _, state = ref.gains(big * np.float32((0.8, 1.0, 1.35)))
    assert state == "applied"


def test_a_look_alike_is_not_mistaken_for_the_region(tmp_path):
    """Measured on 2026-09-28's real stacks: two hours after the reference, the
    picked building's lit windows had changed, and a local match locked onto a
    look-alike block 90 px away at 0.93 -- with the camera not moved at all -- and
    restored the wrong patch's colour. The camera moves as a whole, so the whole
    view decides where the region went."""
    rng = np.random.default_rng(4)
    img = np.zeros((270, 480, 3), np.float32) + np.float32((1.0, 1.1, 1.3))
    img[:110] += cv2.GaussianBlur(rng.uniform(0, 8, (110, 480)).astype(np.float32),
                                  (0, 0), 6)[..., None]                     # cloud
    blocks = range(120, 390, 36)                                  # alike blocks, 36 px apart
    for x0 in blocks:
        img[140:250, x0:x0 + 30] = (14, 12, 10)
        for wy in range(150, 240, 16):
            img[wy:wy + 7, x0 + 8:x0 + 22] = (80, 60, 30)
    img += rng.normal(0, 0.15, img.shape).astype(np.float32)
    x0 = 228                                                      # one block, the picked one
    region = (x0 / 480, 140 / 270, (x0 + 30) / 480, 250 / 270)
    ref = _recorded(tmp_path, img, region)
    later = img.copy()
    for wy in range(150, 240, 16):
        later[wy:wy + 7, x0 + 8:x0 + 22] = (14, 12, 10)          # its windows go dark
    later[140:250, x0:x0 + 30] *= np.float32((1.3, 1.0, 0.7))     # its own light is warmer now
    cast = np.float32((0.8, 1.0, 1.35))
    gains, state = ref.gains(later * cast)
    assert state == "applied"
    # Restored to the picked block's colour at its real place, not a neighbour's.
    want = np.median(img[140:250, x0:x0 + 30].reshape(-1, 3), axis=0)
    got = np.median((later * cast * gains)[140:250, x0:x0 + 30].reshape(-1, 3), axis=0)
    assert np.allclose(got / got[1], want / want[1], atol=0.05), (got / got[1], want / want[1])
