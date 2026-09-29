"""The sensor's fixed grain: recorded once from a flat field, subtracted from night stills.

Measured 2026-09-28. At a stack depth of ~600 frames the grain left in the night
sky was about 90% a pattern fixed to the sensor's pixels: two independent
half-stacks matched at 0.91, and two captures 25 minutes apart and at different
focus at 0.92. Stacking averages away only what changes from frame to frame, so no
depth could touch it -- the smoothing in `render` was hiding it, and that is what
painted the sky into blotches.

A covered-lens dark frame could not capture it: iOS clips everything below black
before encoding, so in the dark ~40% of pixels sit at 0 and the pattern is flattened
away (correlation with the sky's 0.07). It only shows once there is a little light
on every pixel. A flat field gives exactly that -- a sheet of tissue over the lens,
lit dimly by the room -- and its pattern matched the sky's at 0.85.

The pattern is the fine texture only (`pattern`), because the broad shading of a
flat field is the tissue and the lighting, not the sensor. It is subtracted at a
scale fitted per still and per channel over the flattest part of the picture, since
its strength follows the phone's gain from night to night; the fit came out at
0.88-0.94 across brightness levels, so one scale per channel is enough.
"""

import logging
import os

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Every value sensor.flat_field can take, for the Home Assistant enum.
FLAT_FIELD_STATES = ("none", "ready", "recording", "rejected_too_dark",
                     "rejected_too_bright", "rejected_not_flat", "rejected_no_frames")

# The texture scale the pattern lives at. The sky's static pattern was about two
# pixels across (a one-pixel shift still correlated 0.69), which is also the scale
# at which it showed as grain; broader shading is left to the black level.
SIGMA = 2.0
# A flat field has to sit clear of the black clamp, where the pattern is flattened
# away, and well below white, where it is no longer the night's pattern. The
# recording measured sat at ~7 on a 0-255 scale; the night sky at 3-5.
MIN_LEVEL = 2.0
MAX_LEVEL = 60.0
# Through tissue the brightness varies only slowly across the frame (~0.01 per
# pixel); a real scene has edges. A recording with any sharp edge is the view,
# not a flat field -- the lens was not covered.
MAX_EDGE = 0.5
# The share of the picture, least textured first, the scale is fitted over.
FIT_SHARE = 40
MAX_SCALE = 1.5


def pattern(stack):
    """The fine texture of an (H, W, 3) image: itself less a blur of itself."""
    x = np.asarray(stack, dtype=np.float32)
    return x - cv2.GaussianBlur(x, (0, 0), SIGMA)


def _robust_sd(x):
    return float(1.4826 * np.median(np.abs(x - np.median(x))))


def check(stack):
    """Why `stack` is not a usable flat field, or None if it is."""
    luma = np.asarray(stack, dtype=np.float32).mean(axis=2)
    level = float(np.median(luma))
    if level < MIN_LEVEL:
        return "too_dark"
    if level > MAX_LEVEL:
        return "too_bright"
    smooth = cv2.GaussianBlur(luma, (0, 0), SIGMA)
    gy, gx = np.gradient(smooth)
    if float(np.percentile(np.hypot(gx, gy), 99)) > MAX_EDGE:
        return "not_flat"
    return None


class FlatField:
    """The recorded pattern, kept on disk so it outlives restarts."""

    def __init__(self, path):
        self.path = path
        self._pattern = None
        self._warned_shape = None
        try:
            self._pattern = np.load(path).astype(np.float32)
        except FileNotFoundError:
            pass
        except Exception as e:
            # Anything: this is built at start, where an exception is a crash loop, and
            # a power cut during a first recording can leave the file empty.
            log.warning("flat field: could not read %s, correcting nothing: %r", path, e)

    @property
    def ready(self):
        return self._pattern is not None

    def record(self, stack):
        """Keep `stack`'s pattern. Returns why it was refused, or None.

        A refused recording leaves the previous one in place: pressing the button
        with the lens uncovered must not throw away a good calibration.
        """
        reason = check(stack)
        if reason is not None:
            return reason
        p = pattern(stack)
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            np.save(f, p.astype(np.float16))
            f.flush()
            os.fsync(f.fileno())      # on disk before it replaces a good one
        os.replace(tmp, self.path)
        self._pattern = p
        return None

    def correct(self, stack, weight):
        """Subtract the pattern from `stack`, scaled to it. Returns
        (corrected, scales per channel or None, grain left as a share of before).

        `weight` is render.night_weight: nothing is done in daylight, where the
        pattern is buried under the signal and a fitted scale would chase scene
        texture instead.
        """
        if self._pattern is None or not weight > 0:
            return stack, None, 1.0
        if self._pattern.shape != np.shape(stack):
            if self._warned_shape != np.shape(stack):
                self._warned_shape = np.shape(stack)
                log.warning("flat field: recorded at %s but stills are %s; not correcting. "
                            "Record it again.", self._pattern.shape, np.shape(stack))
            return stack, None, 1.0
        texture = pattern(stack)
        # Fitted where the picture itself has least texture, so that what the fit
        # sees is the pattern rather than the scene.
        busy = cv2.GaussianBlur(np.abs(texture).mean(axis=2), (0, 0), 4.0)
        quiet = busy < np.percentile(busy, FIT_SHARE)
        out = np.array(stack, dtype=np.float32, copy=True)
        scales = []
        for c in range(3):
            p = self._pattern[..., c][quiet]
            t = texture[..., c][quiet]
            k = float(np.clip(p @ t / max(float(p @ p), 1e-9), 0.0, MAX_SCALE))
            out[..., c] -= weight * k * self._pattern[..., c]
            scales.append(round(k, 3))
        before = _robust_sd(texture.mean(axis=2)[quiet])
        after = _robust_sd(pattern(out).mean(axis=2)[quiet])
        grain = float(np.clip(after / before, 0.0, 1.0)) if before > 1e-9 else 1.0
        return out, scales, grain
