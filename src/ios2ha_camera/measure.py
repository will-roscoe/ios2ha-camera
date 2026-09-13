"""Pure numpy measurement functions over decoded frames."""
import math

import numpy as np


def black_level(mean):
    """Estimate black level as the 0.5th percentile per channel."""
    return np.array([np.percentile(mean[..., c], 0.5) for c in range(3)], dtype=np.float64)


def luminance(mean, black):
    """Luminance above black level, as an (H, W) float array.

    One definition shared by every luminance figure the service publishes, so
    the mean, the percentiles and the subject level cannot drift apart.
    """
    return np.clip(mean - black, 0, None).mean(axis=2)


def luminance_percentiles(mean, black, pcts=(0.5, 1, 10, 50, 90, 99, 99.5)):
    """Compute luminance percentiles above black level.

    Args:
        mean: (H, W, 3) float array in 0..255
        black: (3,) black level per channel
        pcts: tuple of percentile values (default includes 0.5 and 99.5 for dynamic range)

    Returns:
        dict mapping percentile value to luminance float
    """
    lum = luminance(mean, black)
    return {p: float(np.percentile(lum, p)) for p in pcts}


def required_depth(subject, target, k):
    """Compute required frame depth for gain law N = ceil(k * G**2).

    Where G = max(target / subject, 1). Returns minimum depth before clamping.
    The caller's clamp applies the floor to 1.
    """
    gain = max(target / max(subject, 1e-6), 1.0)
    return int(math.ceil(k * gain * gain))


def motion_level(a, b):
    """Compute mean absolute luminance difference between frames."""
    return float(np.mean(np.abs(a.mean(axis=2) - b.mean(axis=2))))


def focus_sharpness(frame):
    """Compute Laplacian variance (Tenengrad) as focus metric."""
    import cv2
    g = frame.mean(axis=2).astype(np.float32)
    return float(cv2.Laplacian(g, cv2.CV_32F).var())


def colour_temperature_k(mean):
    """Estimate colour temperature in Kelvin from bright pixels.

    Uses CIE 1931 XYZ conversion and inverse McCamy formula.
    """
    lin = np.where(mean / 255 <= 0.04045, mean / 255 / 12.92, ((mean / 255 + 0.055) / 1.055) ** 2.4)
    lum = lin.mean(axis=2)
    bright = lum >= np.percentile(lum, 80)
    if bright.sum() == 0:
        bright = np.ones_like(lum, bool)
    R, G, B = (lin[..., i][bright].mean() for i in range(3))
    X = 0.4124*R + 0.3576*G + 0.1805*B
    Y = 0.2126*R + 0.7152*G + 0.0722*B
    Z = 0.0193*R + 0.1192*G + 0.9505*B
    s = X + Y + Z
    if s <= 0:
        return float("nan")
    x, y = X / s, Y / s
    n = (x - 0.3320) / (0.1858 - y)
    return float(449*n**3 + 3525*n**2 + 6823.3*n + 5520.33)
