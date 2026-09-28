"""Auto horizon: estimate how far the view is rolled, and level stills by it.

Will asked for "some level of auto horizon, activated by manual trigger". The camera
looks through a window at buildings, and the cue is their verticals: building edges,
window frames. Rooflines are no use. Measured on 28 Sep stills, each facade's roofline
leant 3 to 8 degrees by perspective alone, depending on its angle to the camera.

Verticals lean for two reasons. The camera's roll leans them all alike. Its pitch
fans them out by an amount that grows steadily from the centre: the live view looks
up about 13 degrees, and its edge verticals lean about 9. So a line
lean = roll + k * x is fitted to every vertical segment, weighted by length and
robust to strays, and the roll is the lean at the centre.

Across 28 Sep, the daytime stills on one framing agreed to within 0.1 degrees (+0.6
to +0.8). Night stills did not: lit windows and reflections in the glass scatter them
by a degree or more. Any view whose fit is that loose is refused rather than guessed.

Levelling rotates about the centre and zooms just enough to leave no corners: at 2
degrees on a 16:9 frame that is about 4% of the field.
"""

import math

import cv2
import numpy as np

# A segment counts if it is within this many degrees of vertical, and at least this
# fraction of the frame's height long.
ALIGNED = 10.0
MIN_LENGTH = 1 / 40
MIN_LINES = 20
# Refused if the segments scatter about the fit by more than this (degrees, robust
# sigma), or if the roll itself is less certain than MAX_ERROR.
MAX_SPREAD = 1.0
MAX_ERROR = 0.2


class NoClearHorizon(Exception):
    """Too few clear verticals, or ones that disagree: no roll worth trusting."""


def estimate_tilt(rgb):
    """The view's roll in degrees, counter-clockwise positive: `level(rgb, tilt)`
    turns it back. Raises NoClearHorizon."""
    luma = cv2.cvtColor(np.asarray(rgb, dtype=np.uint8), cv2.COLOR_RGB2GRAY)
    h, w = luma.shape
    segments = cv2.createLineSegmentDetector().detect(luma)[0]
    lean, length, x = [], [], []
    for x1, y1, x2, y2 in (segments.reshape(-1, 4) if segments is not None else []):
        size = math.hypot(x2 - x1, y2 - y1)
        # degrees from vertical, positive when the top of the line is to the left
        a = (math.degrees(math.atan2(x2 - x1, y2 - y1)) + 90) % 180 - 90
        if size >= h * MIN_LENGTH and abs(a) <= ALIGNED:
            lean.append(a)
            length.append(size)
            x.append(((x1 + x2) / 2 - w / 2) / w)
    if len(lean) < MIN_LINES:
        raise NoClearHorizon(f"only {len(lean)} clear verticals")
    roll, spread, error = _fit(np.array(lean), np.array(length), np.array(x))
    if spread > MAX_SPREAD:
        raise NoClearHorizon(f"verticals disagree by {spread:.1f} degrees")
    if error > MAX_ERROR:
        raise NoClearHorizon(f"roll uncertain by {error:.2f} degrees")
    return round(roll, 2)


def _fit(lean, length, x, rounds=6):
    """Robust length-weighted fit of lean = roll + k * x: (roll, spread, error)."""
    a = np.stack([np.ones_like(x), x], axis=1)
    weight = length
    for _ in range(rounds):
        root = np.sqrt(weight)
        coef = np.linalg.lstsq(a * root[:, None], lean * root, rcond=None)[0]
        residual = lean - a @ coef
        spread = 1.4826 * float(np.median(np.abs(residual))) + 1e-6
        # Huber: a segment far off the fit counts for less, never for nothing
        weight = length * np.clip(1.5 * spread / (np.abs(residual) + 1e-9), 0, 1)
    share = weight / weight.sum()
    mean_x = float((share * x).sum())
    var_x = float((share * (x - mean_x) ** 2).sum())
    effective = weight.sum() ** 2 / (weight ** 2).sum()
    error = spread * math.sqrt((1 + mean_x ** 2 / max(var_x, 1e-9)) / effective)
    return float(coef[0]), spread, error


def level(rgb, tilt):
    """`rgb` turned back by `tilt` degrees about its centre, zoomed just enough that
    no corner shows. The same object when there is nothing to do."""
    if abs(tilt) < 1e-6:
        return rgb
    h, w = rgb.shape[:2]
    theta = math.radians(abs(tilt))
    scale = math.cos(theta) + max(w / h, h / w) * math.sin(theta)
    m = cv2.getRotationMatrix2D((w / 2, h / 2), -tilt, scale)
    return cv2.warpAffine(rgb, m, (w, h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)
