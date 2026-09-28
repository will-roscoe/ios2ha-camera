"""Far-field autofocus: find the sharpest lens position by measuring it, far side first.

Measured 2026-09-28 on a night skyline through a window: the phone's own autofocus
parked the lens at 0.68 while edge detail peaked at 0.78-0.82, about five times
higher, and by 23:00 the peak had drifted to 0.77. So the service measures instead
of trusting the phone.

A single night frame is too dark to judge edges on, so every probe is an average
of ~35 frames (`average`). The score is the edge energy of that average in the most
detailed quarter of the picture, with the border left out (`score`): the skyline
drives it, flat sky and noise cannot dilute it, and the curtain and window frame at
the edges cannot pull it. Nothing is tied to a particular view.

The search (`search`) looks only between NEAR and FAR. The window and a curtain
within a metre of the lens focus below NEAR, so they are excluded by construction;
FAR is the far end of the lens's travel, past infinity. It probes far to near,
brackets the far-most peak within NEAR_EQUAL of the best -- near-equal peaks go to
the far one -- and narrows the bracket by golden-section comparison. A curve with
no clear peak (fog, darkness, a covered lens) raises NoClearPeak and changes nothing.
"""

import io
import math

import cv2
import numpy as np
from PIL import Image

# Every value sensor.focus_run can take, for the Home Assistant enum.
FOCUS_RUN_STATES = ("idle", "running", "focused", "unchanged_no_clear_peak",
                    "failed_no_frames", "failed_phone")

NEAR = 0.60
FAR = 1.00
# Probed first, far to near.
COARSE = (1.00, 0.90, 0.80, 0.70, 0.60)
# Scores within this share of the best count as a peak as good as it.
NEAR_EQUAL = 0.10
# A coarse spread below this share of the best is no peak at all.
FLAT = 0.15
# The bracket is narrowed until it is this wide.
RESOLUTION = 0.01
# Tiles are 32 px on the half-size decode, 64 px on the full frame.
TILE = 32
BORDER = 0.10
_G = (math.sqrt(5) - 1) / 2


class NoClearPeak(Exception):
    """No position was clearly sharper than the others; `curve` says what was seen."""

    def __init__(self, curve):
        super().__init__(f"no clear peak in {curve}")
        self.curve = curve


def _luma_half(frame):
    """A frame's luma at half size, decoded straight from the JPEG's DCT scaling."""
    im = Image.open(io.BytesIO(frame.data))
    im.draft("L", (im.width // 2, im.height // 2))
    return np.asarray(im.convert("L"), dtype=np.float32)


def average(frames):
    """The mean half-size luma of `frames`, as float32."""
    total = None
    for f in frames:
        a = _luma_half(f)
        total = a.astype(np.float64) if total is None else total + a
    return (total / len(frames)).astype(np.float32)


def score(luma):
    """Edge energy of the most detailed quarter of `luma`, border excluded."""
    h, w = luma.shape
    by, bx = int(h * BORDER), int(w * BORDER)
    core = np.asarray(luma, dtype=np.float32)[by:h - by, bx:w - bx]
    lap = cv2.Laplacian(cv2.GaussianBlur(core, (0, 0), 1.0), cv2.CV_32F)
    ny, nx = lap.shape[0] // TILE, lap.shape[1] // TILE
    tiles = lap[:ny * TILE, :nx * TILE].reshape(ny, TILE, nx, TILE).var(axis=(1, 3))
    k = max(1, tiles.size // 4)
    return float(np.sort(tiles.ravel())[-k:].mean())


def search(measure):
    """Find the sharpest far-field position. Returns (position, curve).

    `measure(position)` moves the lens there and returns its score; it is called
    at most once per position. `curve` maps every position probed to its score.
    """
    curve = {}

    def m(p):
        p = round(min(FAR, max(NEAR, p)), 4)
        if p not in curve:
            curve[p] = measure(p)
        return curve[p]

    for p in COARSE:
        m(p)
    best = max(curve.values())
    if not best > 0 or (best - min(curve.values())) / best < FLAT:
        raise NoClearPeak(curve)

    # The far-most coarse probe as good as the best: the far bias.
    i = next(i for i, p in enumerate(COARSE) if curve[p] >= (1 - NEAR_EQUAL) * best)
    hi = COARSE[max(i - 1, 0)]
    lo = COARSE[min(i + 1, len(COARSE) - 1)]

    # Golden-section: one new probe per step, ties kept on the far side.
    a, b = lo, hi
    c, d = b - _G * (b - a), a + _G * (b - a)
    fc, fd = m(c), m(d)
    while b - a > RESOLUTION:
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - _G * (b - a)
            fc = m(c)
        else:
            a, c, fc = c, d, fd
            d = a + _G * (b - a)
            fd = m(d)

    inside = [p for p in curve if lo - 1e-9 <= p <= hi + 1e-9]
    top = max(curve[p] for p in inside)
    # The best measured position in the bracket, ties -- within 2% -- to the far side.
    position = max(p for p in inside if curve[p] >= 0.98 * top)
    return position, curve
