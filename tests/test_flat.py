"""Fixed-pattern grain: measured once from a flat field, subtracted from night stills.

Measured 2026-09-28: at a stack depth of ~600 frames the grain left in the night
sky was ~90% a pattern fixed to the sensor's pixels -- two independent half-stacks
matched at 0.91, two captures 25 minutes apart and at different focus at 0.92 --
so no amount of stacking could remove it. A covered-lens dark frame could not
capture it (iOS clips below black, correlation 0.07); a flat field could: a sheet
of tissue over the lens, lit dimly by the room, matched the sky's pattern at 0.85.
"""
import numpy as np
import pytest

from ios2ha_camera.flat import FlatField, check, pattern


def _scene(h=120, w=160, seed=0):
    """A night scene: smooth sky with a lit building, at night levels."""
    rng = np.random.default_rng(seed)
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    sky = 3.0 + 0.01 * y
    img = np.repeat(sky[..., None], 3, axis=2)
    img[70:, 40:100] = 12.0                                   # a building
    img[80:90, 50:60] = 60.0                                  # a lit window
    return img, rng


def _fixed_pattern(h=120, w=160, seed=1, amp=0.15):
    rng = np.random.default_rng(seed)
    return (rng.normal(0, amp, (h, w, 3))).astype(np.float32)


def test_the_pattern_is_the_fine_texture_only():
    img, _ = _scene()
    fpn = _fixed_pattern()
    p = pattern(img + fpn)
    # the scene's broad shading is gone; what is left is mostly the pattern
    sky = (slice(5, 60), slice(5, 150))
    assert abs(float(p[sky].mean())) < 0.02
    assert np.corrcoef(p[sky].ravel(), fpn[sky].ravel())[0, 1] > 0.8


def test_a_flat_field_is_accepted():
    flat = np.full((120, 160, 3), 7.0, np.float32) + _fixed_pattern()
    assert check(flat) is None


@pytest.mark.parametrize("level, expected", [(0.8, "too_dark"), (90.0, "too_bright")])
def test_a_flat_field_must_sit_above_black_and_well_below_white(level, expected):
    """Below the clamp the pattern is flattened away -- which is why a covered-lens
    dark frame failed; near white it is no longer the night's pattern."""
    flat = np.full((120, 160, 3), level, np.float32) + _fixed_pattern()
    assert check(flat) == expected


def test_a_scene_is_not_a_flat_field():
    """Pressing the button with the lens uncovered must not record the view."""
    img, _ = _scene()
    assert check(img + _fixed_pattern()) == "not_flat"


def _record(tmp_path, fpn):
    ff = FlatField(str(tmp_path / "flat.npy"))
    assert ff.record(np.full(fpn.shape, 7.0, np.float32) + fpn) is None
    return ff


def test_correction_removes_the_pattern_and_keeps_the_scene(tmp_path):
    fpn = _fixed_pattern()
    ff = _record(tmp_path, fpn)
    img, rng = _scene()
    noisy = img + 0.9 * fpn + rng.normal(0, 0.03, img.shape).astype(np.float32)
    out, scales, grain = ff.correct(noisy, weight=1.0)
    assert all(0.8 < k < 1.0 for k in scales)
    sky = (slice(5, 60), slice(5, 150))
    before = np.std(pattern(noisy)[sky]); after = np.std(pattern(out)[sky])
    assert after < 0.5 * before
    assert grain == pytest.approx(after / before, rel=0.25)
    # the window and the building edge are untouched in scale
    assert abs(float(out[80:90, 50:60].mean()) - 60.0) < 0.2


def test_no_correction_in_daylight_or_without_a_recording(tmp_path):
    img, _ = _scene()
    ff = FlatField(str(tmp_path / "absent.npy"))
    out, scales, grain = ff.correct(img, weight=1.0)
    assert out is img and scales is None and grain == 1.0
    ff = _record(tmp_path, _fixed_pattern())
    out, scales, grain = ff.correct(img, weight=0.0)
    assert out is img and scales is None and grain == 1.0


def test_a_recording_of_another_size_is_ignored(tmp_path, caplog):
    """A different phone or resolution: the pattern belongs to other pixels."""
    ff = _record(tmp_path, _fixed_pattern())
    img = np.full((60, 80, 3), 3.0, np.float32)
    with caplog.at_level("WARNING"):
        out, scales, _ = ff.correct(img, weight=1.0)
    assert out is img and scales is None


def test_the_recording_survives_a_restart(tmp_path):
    fpn = _fixed_pattern()
    _record(tmp_path, fpn)
    again = FlatField(str(tmp_path / "flat.npy"))
    assert again.ready
    img, rng = _scene()
    _, scales, _ = again.correct(img + 0.9 * fpn, weight=1.0)
    assert scales is not None


def test_a_rejected_recording_keeps_the_previous_one(tmp_path):
    fpn = _fixed_pattern()
    ff = _record(tmp_path, fpn)
    img, _ = _scene()
    assert ff.record(img) == "not_flat"
    assert FlatField(str(tmp_path / "flat.npy")).ready
