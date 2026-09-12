"""Turn a stacked float image into the display JPEG that Home Assistant shows.

Two render intents, chosen by comparing them on real night footage. `natural`
(the default) is photographic tone reproduction keyed to the scene's
geometric-mean luminance -- low key, the way a real low-light photograph
looks, with darker tones progressively desaturated as eyes lose colour
vision in dim light. `inspection` is an asinh stretch to near full range for
reading detail in the still, and is expected to look processed rather than
natural.

Every tone curve here derives ONE scale factor per pixel from luminance and
multiplies all three channels by that same factor -- it never looks the
curve up per channel. That is what keeps hue intact through tone mapping:
looking the curve up separately per channel would compress each channel by
a different amount and shift or wash out colour.
"""
import io
import numpy as np
from PIL import Image, ImageFilter
from .measure import black_level

# Push the inspection stretch slightly past the measured highlight so genuinely
# bright content clips to display white -- "near full range", in contrast to
# natural's low-key exposure, which is meant to hold headroom below white.
_INSPECTION_HEADROOM = 1.15


def white_balance_gains(linear, mode):
    """Per-channel gain for `linear`, an array already black-level-subtracted.

    `mode` is one of as-shot | grey-world | brightest-patch.
    """
    if mode == "as-shot":
        return np.ones(3)
    if mode == "grey-world":
        m = linear.reshape(-1, 3).mean(axis=0)
        return m.mean() / np.maximum(m, 1e-6)
    if mode == "brightest-patch":
        hi = np.array([np.percentile(linear[..., c], 99.5) for c in range(3)])
        return hi.mean() / np.maximum(hi, 1e-6)
    return np.ones(3)


def _scale_to_luminance(x, L, target):
    """Multiply every channel of x by the one factor that takes L to target.

    x and target must already be in the same units as L. Both tone curves
    route through this: computing `target` once from L and applying it
    identically to R, G and B is the shared-factor step that preserves hue.
    """
    scale = np.where(L > 1e-6, target / np.maximum(L, 1e-6), 0.0)[..., None]
    return x * scale


def _tone_natural(x, L, night_key):
    """Photographic tone reproduction keyed to the scene's geometric-mean luminance."""
    Lavg = np.exp(np.mean(np.log(1e-3 + L)))
    Ls = night_key / Lavg * L
    Lw = np.percentile(Ls, 99.9)
    Ld = Ls * (1 + Ls / max(Lw, 1e-6) ** 2) / (1 + Ls)
    rgb = np.clip(_scale_to_luminance(x, L, Ld), 0, 1) ** (1 / 2.2)
    Y = rgb.mean(axis=2, keepdims=True)
    # Darker tones desaturate towards grey, the way eyes lose colour vision in dim light.
    return Y + (rgb - Y) * np.clip(Y / 0.4, 0.4, 1.0)


def _tone_inspection(x, L, strength):
    """Asinh stretch to near full range; meant to look processed, not natural."""
    lo, hi = np.percentile(L, 0.5), np.percentile(L, 99.8)
    span = max(hi - lo, 1e-6)
    y = np.arcsinh(np.clip((L - lo) / span, 0, None) * strength) / np.arcsinh(strength)
    target = y * span + lo
    lin = (_scale_to_luminance(x, L, target) - lo) / span * _INSPECTION_HEADROOM
    return np.clip(lin, 0, 1) ** (1 / 2.2)


def calibration(stacked, settings):
    """The black level and white-balance gains render() will apply to `stacked`.

    Exposed because the supervisor publishes both as Home Assistant sensors:
    computing them here once, and handing the result back to render(), is what
    keeps the published numbers identical to the applied ones.
    """
    bl = np.asarray(settings.black_level_override) if settings.black_level_override else black_level(stacked)
    gains = white_balance_gains(np.clip(stacked - bl, 0, None), settings.white_balance)
    return bl, gains


def render(stacked, settings, applied=None):
    """Render a stacked (H, W, 3) float array to a display-ready uint8 RGB array.

    `applied` is an optional (black_level, gains) pair from calibration(), for a
    caller that also publishes those numbers and should not compute them twice.
    """
    bl, gains = applied if applied is not None else calibration(stacked, settings)
    x = np.clip(stacked - bl, 0, None)
    x = x * gains
    L = x.mean(axis=2)
    if settings.render_intent == "inspection":
        rgb = _tone_inspection(x, L, settings.stretch_strength)
    else:
        rgb = _tone_natural(x, L, settings.night_key)
    img8 = (np.clip(rgb, 0, 1) * 255).astype(np.uint8)
    if settings.chroma_radius > 0:
        y, cb, cr = Image.fromarray(img8).convert("YCbCr").split()
        r = settings.chroma_radius
        img8 = np.asarray(Image.merge("YCbCr", (
            y, cb.filter(ImageFilter.GaussianBlur(r)), cr.filter(ImageFilter.GaussianBlur(r)),
        )).convert("RGB"))
    if settings.denoise_strength > 0:
        import cv2
        h = float(settings.denoise_strength)
        img8 = cv2.fastNlMeansDenoisingColored(img8, None, h=h, hColor=h + 1,
                                               templateWindowSize=7, searchWindowSize=21)
    return img8


def to_jpeg(rgb, quality=92):
    """Encode an (H, W, 3) uint8 RGB array as JPEG bytes."""
    b = io.BytesIO()
    Image.fromarray(rgb).save(b, "JPEG", quality=quality)
    return b.getvalue()
