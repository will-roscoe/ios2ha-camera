"""Auto horizon: estimate the view's roll from its vertical lines, and level it.

Will asked for "some level of auto horizon, activated by manual trigger". Through a
window at buildings the cue is the verticals -- building edges, window frames. Rooflines
are no use: on 28 Sep stills each receding facade leant 3 to 8 degrees by perspective
alone. A camera pitched up or down makes the verticals fan out, by an amount that grows
steadily away from the centre, so the roll is the lean of a vertical at the centre.
"""
import cv2
import numpy as np
import pytest

from ios2ha_camera import horizon


def _city(h=540, w=960, seed=0):
    """Buildings: rectangles with window grids, square to the frame."""
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), 40, np.uint8)
    for i in range(7):
        x0 = 40 + i * 130
        top = int(rng.integers(150, 300))
        cv2.rectangle(img, (x0, top), (x0 + 100, h - 1), (120, 115, 110), -1)
        for wy in range(top + 15, h - 20, 28):
            for wx in range(x0 + 12, x0 + 90, 22):
                cv2.rectangle(img, (wx, wy), (wx + 12, wy + 14), (220, 200, 150), -1)
    return img


def _rolled(img, tilt):
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), tilt, 1.0)
    return cv2.warpAffine(img, m, (w, h), borderMode=cv2.BORDER_REFLECT)


def _pitched(img, pitch_deg, hfov_deg=69.0):
    """The view from a camera pitched up by `pitch_deg`: verticals fan out."""
    h, w = img.shape[:2]
    f = (w / 2) / np.tan(np.radians(hfov_deg / 2))
    k = np.array([[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]])
    p = np.radians(pitch_deg)
    r = np.array([[1, 0, 0], [0, np.cos(p), -np.sin(p)], [0, np.sin(p), np.cos(p)]])
    return cv2.warpPerspective(img, k @ r @ np.linalg.inv(k), (w, h),
                               borderMode=cv2.BORDER_REFLECT)


@pytest.mark.parametrize("tilt", [0.0, 0.7, -2.3, 4.0])
def test_the_roll_is_recovered(tilt):
    assert horizon.estimate_tilt(_rolled(_city(), tilt)) == pytest.approx(tilt, abs=0.2)


def test_a_pitched_camera_is_not_mistaken_for_a_rolled_one():
    """Measured on the live view: pitched up ~13 degrees, edge verticals lean ~9."""
    view = _rolled(_pitched(_city(), 12.0), 0.8)
    assert horizon.estimate_tilt(view) == pytest.approx(0.8, abs=0.2)


def test_a_view_with_no_lines_is_refused():
    rng = np.random.default_rng(1)
    noise = cv2.GaussianBlur(rng.integers(0, 255, (540, 960, 3)).astype(np.uint8), (0, 0), 3)
    with pytest.raises(horizon.NoClearHorizon):
        horizon.estimate_tilt(noise)


def _verticals(leans, h=540, w=960):
    img = np.full((h, w, 3), 30, np.uint8)
    xs = np.linspace(80, w - 80, len(leans))
    for x, a in zip(xs, leans, strict=True):
        dx = 200 * np.sin(np.radians(a))
        cv2.line(img, (int(x - dx), 70), (int(x + dx), h - 70), (230, 230, 230), 3)
    return img


def test_verticals_that_disagree_are_refused():
    """Neighbours leaning 6 degrees opposite ways: no single roll explains them."""
    with pytest.raises(horizon.NoClearHorizon):
        horizon.estimate_tilt(_verticals([6, -6] * 12))


def test_rooflines_alone_are_not_trusted():
    """Horizontal edges lean with every facade's angle to the camera, so a view of
    only horizontals is refused rather than levelled on perspective."""
    img = np.full((540, 960, 3), 30, np.uint8)
    for i in range(12):
        y = 40 + i * 38
        cv2.line(img, (100, y), (860, y + 10), (230, 230, 230), 3)
    with pytest.raises(horizon.NoClearHorizon):
        horizon.estimate_tilt(img)


def test_levelling_undoes_the_roll():
    tilted = _rolled(_city(), 2.0)
    levelled = horizon.level(tilted, horizon.estimate_tilt(tilted))
    assert levelled.shape == tilted.shape
    assert abs(horizon.estimate_tilt(levelled)) < 0.3


def test_levelling_leaves_no_black_corners():
    img = np.full((540, 960, 3), 200, np.uint8)
    out = horizon.level(img, 3.0)
    assert out.min() > 150


def test_no_rotation_is_no_change():
    img = _city()
    assert horizon.level(img, 0.0) is img


def test_levelling_keeps_the_detail():
    """Measured on a real still at 0.7 degrees: bilinear resampling cost 45% of the
    sharpness score, which reads as the focus drifting."""
    from ios2ha_camera import measure
    rng = np.random.default_rng(2)
    img = np.clip(_city().astype(np.float32) + rng.normal(0, 12, (540, 960, 3)), 0, 255)
    img = cv2.GaussianBlur(img, (0, 0), 0.8)
    before = measure.focus_sharpness(img)
    after = measure.focus_sharpness(horizon.level(img, 0.7))
    assert after > 0.8 * before
