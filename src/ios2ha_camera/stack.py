"""JPEG decode and sigma-clipped mean stacking, plus the depth law.

Stacking is mean-family only. A median posterises a dim subject (it sits at
0-2 out of 255 per frame in low light) into flat patches -- sigma-clipped mean
keeps the continuous gradient a straight mean would give while still rejecting
outlier pixels (sensor noise spikes, a passing light) that a plain mean would not.
"""
import math
import numpy as np
from PIL import Image
import io
from .measure import required_depth


def decode(frame):
    """Decode a compressed Frame into an (H, W, 3) float32 array in 0..255."""
    return np.asarray(Image.open(io.BytesIO(frame.data)).convert("RGB"), dtype=np.float32)


def sigma_clipped_mean(arrays, sigma=2.5, passes=2, chunk=64):
    """Mean-family stack of decoded frames, iteratively rejecting per-pixel outliers.

    Processed in row chunks so a large stack of full-resolution frames doesn't
    require materialising every intermediate array at once.
    """
    stack_arr = np.stack(arrays)
    H = stack_arr.shape[1]
    out = np.empty(stack_arr.shape[1:], dtype=np.float64)
    for r in range(0, H, chunk):
        ch = stack_arr[:, r:r + chunk].astype(np.float32)
        mu = ch.mean(axis=0)
        sd = ch.std(axis=0) + 1e-6
        for _ in range(passes):
            keep = np.abs(ch - mu) < sigma * sd
            cnt = keep.sum(axis=0).clip(1)
            mu = (ch * keep).sum(axis=0) / cnt
            sd = np.sqrt((((ch - mu) ** 2) * keep).sum(axis=0) / cnt) + 1e-6
        out[r:r + chunk] = mu
    return out


def stack_frames(frames):
    """Decode and sigma-clip-mean a list of Frames into one (H, W, 3) array."""
    return sigma_clipped_mean([decode(f) for f in frames])


def choose_depth(subject, settings, fps, previous_depth):
    """Pick the stack depth for the next capture interval.

    `subject` is the measured subject luminance driving the depth law. Depth
    is clamped to settings.max_depth and to fps * window_max_s (so a stack
    never spans longer than window_max_s seconds of real time at the live
    frame rate), then smoothed towards that target with an EMA over
    ramp_tau_s so depth doesn't jump discontinuously between intervals.
    `previous_depth` is None on the first interval -- there is nothing to
    ramp from yet, so the target is returned directly rather than needing an
    arbitrary seed value to smooth from.
    """
    if settings.stacking_mode == "off" or not settings.stacking_allowed:
        return 1
    if settings.stacking_mode == "fixed":
        target_depth = int(settings.fixed_depth)
    else:
        target_depth = required_depth(subject, settings.target_level, settings.clean_factor)
    hard_cap = min(int(settings.max_depth), int(fps * settings.window_max_s))
    target_depth = max(1, min(target_depth, hard_cap))
    if previous_depth is None or settings.interval_s <= 0:
        return target_depth
    alpha = 1 - math.exp(-settings.interval_s / max(settings.ramp_tau_s, 1e-6))
    return max(1, min(hard_cap, round(previous_depth + alpha * (target_depth - previous_depth))))
