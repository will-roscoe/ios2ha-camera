"""JPEG decode and sigma-clipped mean stacking, plus the depth law.

Stacking is mean-family only. A median posterises a dim subject (it sits at
0-2 out of 255 per frame in low light) into flat patches -- sigma-clipped mean
keeps the continuous gradient a straight mean would give while still rejecting
outlier pixels (sensor noise spikes, a passing light) that a plain mean would not.

Stacking depth reaches max_depth=600 full-resolution frames at night, and a
decoded 1080x1920 frame is ~25 MB -- holding every decoded frame in memory at
once would need ~15 GB, more than this host has. `stack_frames` instead
accumulates sum/sum-of-squares/count per pixel one decoded frame at a time
(see `_accumulate_pass`), so memory stays bounded by a handful of decoded
frames plus the accumulators, independent of how many frames are stacked.
"""
import io
import itertools
import math
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PIL import Image

from .measure import required_depth


def decode(frame):
    """Decode a compressed Frame into an (H, W, 3) float32 array in 0..255."""
    return np.asarray(Image.open(io.BytesIO(frame.data)).convert("RGB"), dtype=np.float32)


def _accumulate_pass(frame_source, mu, sd, sigma):
    """One sigma-clip pass, accumulating over whatever frame_source() yields.

    mu=None means unclipped -- this is the initial plain mean/std pass, with
    every pixel counted. Otherwise a pixel is excluded from this pass's sums
    when it sits `sigma` standard deviations or more from the previous pass's
    mu, matching the batch algorithm's clip-then-recompute step exactly.

    Accumulators are float64 throughout, including the sum of squares: float32
    only holds integer values exactly up to 2**24, and max_depth=600 frames at
    255**2 per pixel alone already exceeds that.
    """
    total = sq_total = count = None
    # sigma and sd are both invariant for the whole pass -- sd is fixed for the
    # call, sigma is a constant -- so this is computed once here rather than
    # recomputed on every frame inside the loop below.
    thresh = None if mu is None else sigma * sd
    for arr in frame_source():
        arr = arr.astype(np.float64, copy=False)
        if total is None:
            total = np.zeros(arr.shape, dtype=np.float64)
            sq_total = np.zeros(arr.shape, dtype=np.float64)
            count = np.zeros(arr.shape, dtype=np.float64)
        keep = np.ones(arr.shape, dtype=bool) if thresh is None else np.abs(arr - mu) < thresh
        total += arr * keep
        sq_total += (arr ** 2) * keep
        count += keep
    count = count.clip(1)
    new_mu = total / count
    # Var(kept) = E[x^2 | kept] - E[x | kept]^2, algebraically the same
    # quantity the batch version gets from sqrt(mean((x - new_mu)**2)).
    variance = np.clip(sq_total / count - new_mu ** 2, 0, None)
    new_sd = np.sqrt(variance) + 1e-6
    return new_mu, new_sd


def _sigma_clipped_mean_core(frame_source, sigma, passes):
    """Shared maths for sigma_clipped_mean and stack_frames.

    frame_source is a zero-arg callable returning a fresh iterator each time
    it's called, since sigma clipping needs `passes + 1` full passes over the
    data (one unclipped, then `passes` clip-and-recompute iterations). Kept
    as the single place the clipping arithmetic lives so the in-memory and
    streaming entry points below cannot drift apart from each other.
    """
    mu = sd = None
    for _ in range(passes + 1):
        mu, sd = _accumulate_pass(frame_source, mu, sd, sigma)
    return mu


def sigma_clipped_mean(arrays, sigma=2.5, passes=2):
    """Sigma-clipped mean over already-decoded arrays held in memory."""
    return _sigma_clipped_mean_core(lambda: iter(arrays), sigma, passes)


def _decode_bounded(frames, workers):
    """Decode frames on a thread pool, keeping at most 2*workers decoded
    frames resident at once instead of eagerly decoding and buffering all of
    them (as executor.map would), so a deep stack never needs more memory
    than a handful of decoded frames regardless of stack depth.
    """
    limit = max(1, 2 * workers)
    # ThreadPoolExecutor, not a process pool (departs from the original spec):
    # PIL's JPEG decode releases the GIL and measured a 4.6x speedup with 4
    # threads on a full-size frame, and threads avoid pickling the ~50 MB
    # accumulators to worker processes and forking a process that already
    # runs other threads.
    with ThreadPoolExecutor(max_workers=workers) as ex:
        pending = deque()
        it = iter(frames)
        for f in itertools.islice(it, limit):
            pending.append(ex.submit(decode, f))
        for f in it:
            yield pending.popleft().result()
            pending.append(ex.submit(decode, f))
        while pending:
            yield pending.popleft().result()


def stack_frames(frames):
    """Decode and sigma-clip-mean a list of Frames into one (H, W, 3) array,
    without ever holding more than a bounded handful of decoded frames in
    memory at once (see _decode_bounded and _accumulate_pass)."""
    workers = min(4, os.cpu_count() or 1)
    return _sigma_clipped_mean_core(lambda: _decode_bounded(frames, workers), 2.5, 2)


def choose_depth(subject, settings, fps, previous_depth, *, hard_cap=None):
    """Pick the stack depth for the next capture interval.

    `subject` is the measured subject luminance driving the depth law. Depth
    is clamped to settings.max_depth and to fps * window_max_s (so a stack
    never spans longer than window_max_s seconds of real time at the live
    frame rate), then smoothed towards that target with an EMA over
    ramp_tau_s so depth doesn't jump discontinuously between intervals.
    `previous_depth` is None on the first interval -- there is nothing to
    ramp from yet, so the target is returned directly rather than needing an
    arbitrary seed value to smooth from.

    `hard_cap` replaces the settings-derived cap for callers whose depth means
    something different from a still's -- the live feed caps smear in seconds,
    not in frames, and must not inherit the stills' 600-frame night depth.
    """
    if settings.stacking_mode == "off" or not settings.stacking_allowed:
        return 1
    if settings.stacking_mode == "fixed":
        target_depth = int(settings.fixed_depth)
    else:
        target_depth = required_depth(subject, settings.target_level, settings.clean_factor)
    if hard_cap is None:
        hard_cap = min(int(settings.max_depth), int(fps * settings.window_max_s))
    hard_cap = max(1, int(hard_cap))
    target_depth = max(1, min(target_depth, hard_cap))
    if previous_depth is None or settings.interval_s <= 0:
        return target_depth
    alpha = 1 - math.exp(-settings.interval_s / max(settings.ramp_tau_s, 1e-6))
    return max(1, min(hard_cap, round(previous_depth + alpha * (target_depth - previous_depth))))
