"""Night colour held to a region of the view: remembered once, restored every still.

Measured 2026-09-28: covering the lens, and later a curtain opening behind the
camera, moved the phone's automatic white balance, and at night there is too little
light for it to come back -- stills stayed cyan (green/red 1.48 against 0.97), then
bluer everywhere. Restarting the app's capture session fixed it once and made it
worse the second time, and the app exposes no white-balance control at all.

So a person picks a region of the view (the colour_region_* controls, drawn on the
control page) and presses Set colour reference on a still that looks right. The
region's colour is remembered, with a small picture of the whole view. Each later
night still is registered against that picture -- the camera "may be adjusted at
times, not a lot" -- and the region is measured where the whole view says it went,
then given the per-channel gains that restore its remembered colour, normalised so
only colour changes, never brightness. A view that no longer matches is left alone
and says so.

The whole view, not the region's own surroundings, decides where it went: measured
on 2026-09-28's real stacks, two hours after the reference the picked building's lit
windows had changed, and matching the region locally locked onto a look-alike block
90 px away at 0.93 correlation while the camera had not moved at all.
"""

import logging
import os

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Every value sensor.colour_reference can take, for the Home Assistant enum.
COLOUR_REFERENCE_STATES = ("none", "ready", "applied", "no_match", "region_too_dark",
                           "rejected_daylight", "rejected_too_dark", "rejected_no_still")

# The picture is matched at about this width, whatever the frame size: plenty to
# find a region by, and cheap.
MATCH_WIDTH = 480
# How far the view may have moved, as a share of the frame.
SEARCH = 0.10
# Once aligned, views correlating less than this are different views, not a moved
# one. Measured 2026-09-28: the same view two hours and many lights apart 0.79;
# flipped or mirrored, -0.07 and -0.11. Phase correlation's own confidence is no
# guide at night -- 0.03-0.08 for the same view, 0.15 for the flipped one -- so it is
# used for the shift only.
MIN_SIMILARITY = 0.5
# A region darker than this, above black, has no colour worth keeping; nor has one
# with any channel below MIN_CHANNEL, which would become a gain of next to nothing.
MIN_LEVEL = 2.0
MIN_CHANNEL = 0.5
MAX_GAIN = 4.0
# A region moved partly out of the frame is measured only if at least this share of
# it is still in view: clamped to the edge, it would be a strip of something else.
MIN_INSIDE = 0.5
_PARTS = {"colour": (3,), "box": (4,), "shape": (2,)}


def _box(shape, region):
    """A region (fractions x0, y0, x1, y1) as pixel bounds, at least a pixel each way."""
    h, w = shape[:2]
    x0, y0, x1, y1 = region
    left, top = int(np.clip(min(x0, x1) * w, 0, w - 1)), int(np.clip(min(y0, y1) * h, 0, h - 1))
    right = int(np.clip(max(x0, x1) * w, left + 1, w))
    bottom = int(np.clip(max(y0, y1) * h, top + 1, h))
    return left, top, right, bottom


def _colour(linear, box):
    left, top, right, bottom = box
    return np.median(linear[top:bottom, left:right].reshape(-1, 3), axis=0).astype(np.float64)


def _measurable(colour):
    return colour.mean() >= MIN_LEVEL and colour.min() >= MIN_CHANNEL


def _complete(ref):
    """Whether a loaded reference has every part, each the right shape."""
    return (all(k in ref and ref[k].shape == shape for k, shape in _PARTS.items())
            and "view" in ref and ref["view"].ndim == 2)


def _view(linear):
    """The view to register by: log luma at MATCH_WIDTH, so a few bright lights
    changing cannot outweigh the shapes of everything else. Also returns the factor
    from it back to the frame."""
    h, w = linear.shape[:2]
    scale = max(1, w // MATCH_WIDTH)
    luma = np.asarray(linear, dtype=np.float32).mean(axis=2)
    if scale > 1:
        luma = cv2.resize(luma, (w // scale, h // scale), interpolation=cv2.INTER_AREA)
    return np.log1p(luma).astype(np.float64), scale


def _similarity(ref_view, view, dx, dy):
    """Correlation of the two views once `view` is shifted back by (dx, dy)."""
    back = cv2.warpAffine(view, np.float64([[1, 0, -dx], [0, 1, -dy]]), view.shape[::-1],
                          flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    m = int(max(abs(dx), abs(dy))) + 2
    a, b = ref_view[m:-m, m:-m].ravel(), back[m:-m, m:-m].ravel()
    if a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


class ColourReference:
    def __init__(self, path):
        self.path = path
        self._ref = None
        try:
            with np.load(path) as data:
                ref = {k: data[k] for k in data.files}
            if not _complete(ref):
                raise ValueError(f"incomplete: has {sorted(ref)}")
            self._ref = ref
        except FileNotFoundError:
            pass
        except Exception as e:
            # Anything: this is built at start, where an exception is a crash loop, and
            # a power cut during a first recording can leave the file empty or cut short.
            log.warning("colour reference: could not read %s, correcting nothing: %r", path, e)

    @property
    def ready(self):
        return self._ref is not None

    def record(self, linear, region):
        """Remember `region`'s colour in `linear` (a black-subtracted stack). Returns
        why it was refused, or None. A refusal keeps the previous reference."""
        box = _box(linear.shape, region)
        colour = _colour(linear, box)
        if not _measurable(colour):
            return "too_dark"
        view, _ = _view(linear)
        ref = {"colour": colour, "view": view, "box": np.array(box),
               "shape": np.array(linear.shape[:2])}
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            np.savez(f, **ref)
            f.flush()
            os.fsync(f.fileno())      # on disk before it replaces a good one
        os.replace(tmp, self.path)
        self._ref = ref
        return None

    def gains(self, linear):
        """Per-channel gains restoring the remembered colour. Returns (gains, state):
        (None, "none") with no reference, (None, "no_match") when the view no longer
        contains the region, (None, "region_too_dark") when its light is out, else
        (gains, "applied")."""
        ref = self._ref                  # once: a recording may replace it meanwhile
        if ref is None:
            return None, "none"
        if tuple(ref["shape"]) != tuple(linear.shape[:2]):
            return None, "no_match"
        view, scale = _view(linear)
        ref_view = ref["view"]
        if view.shape != ref_view.shape:
            return None, "no_match"
        window = cv2.createHanningWindow(view.shape[::-1], cv2.CV_64F)
        (dx, dy), _ = cv2.phaseCorrelate(ref_view, view, window)
        sh, sw = view.shape
        if abs(dx) > sw * SEARCH or abs(dy) > sh * SEARCH:
            return None, "no_match"
        if _similarity(ref_view, view, dx, dy) < MIN_SIMILARITY:
            return None, "no_match"
        dx, dy = round(dx * scale), round(dy * scale)
        left, top, right, bottom = (int(v) for v in ref["box"])
        h, w = linear.shape[:2]
        moved = (int(np.clip(left + dx, 0, w - 1)), int(np.clip(top + dy, 0, h - 1)),
                 int(np.clip(right + dx, 1, w)), int(np.clip(bottom + dy, 1, h)))
        inside = ((moved[2] - moved[0]) * (moved[3] - moved[1])
                  / max(1, (right - left) * (bottom - top)))
        if inside < MIN_INSIDE:
            return None, "no_match"
        now = _colour(linear, moved)
        if not _measurable(now):
            return None, "region_too_dark"
        want = ref["colour"]
        gains = (want / now) * (now.mean() / want.mean())
        return np.clip(gains, 1 / MAX_GAIN, MAX_GAIN), "applied"
