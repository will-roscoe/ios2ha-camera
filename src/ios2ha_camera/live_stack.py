"""A live feed with the stills' low-light treatment, at a rate a shared box can hold.

The stills' renderer is far too slow for video: `render.render` measured 1.9 s a
frame at full resolution with its defaults and 424 ms with the denoise and
chroma blur off, against a 72 ms budget at 13.9 fps. This module keeps the same
arithmetic and buys the speed elsewhere -- half resolution through the JPEG's
own DCT scaling, an exponential moving average instead of a stack of N frames,
and tone parameters estimated from a sixteenth of the pixels twice a second.
Measured against `render.render`, the difference is at most 2/255.

The moving average is what "older frames count less" means, and it is O(1) in
time and memory: `acc += (new - acc) * a`. `a` is `max(1/t, 1/N)`, where `t`
counts frames since the last reset and `N` is the chosen depth -- a plain
running mean for the first `N` frames (frame 1 alone, then the mean of 2, then
of 3), settling into an exponential moving average with `a = 1/N` once `t`
passes `N`, so older frames are down-weighted rather than remembered forever.
"""

import io
import logging
import threading
import time

import cv2
import numpy as np
from PIL import Image

from . import render, stack
from .ring_buffer import Frame

log = logging.getLogger(__name__)

SCALE = 2               # 1920x1080 -> 960x540; full resolution measured 4 fps and is not offered
REFRESH_S = 2.0         # how often tone parameters and the depth are re-estimated
GAP_RESET_S = 2.0       # a longer gap means a new session; never blend across it
SUBSAMPLE = 4           # 1/16 of the pixels for parameter estimation
JPEG_QUALITY = 85
POLL_S = 0.02
GAMMA = 1 / 2.2


def _decode_scaled(data):
    """Decode straight to half size: PIL's draft() uses the JPEG's DCT scaling,
    which measured 6.5 ms against 16.5 ms for a full decode."""
    im = Image.open(io.BytesIO(data))
    im.draft("RGB", (im.width // SCALE, im.height // SCALE))
    return np.asarray(im.convert("RGB"), dtype=np.float32)


class LiveStacker:
    def __init__(self, buffer, settings):
        self.buffer, self.s = buffer, settings
        self._acc = None
        self._t = 0
        self._last_frame_ts = None
        self._depth = 1
        self._params = None
        self._params_at = 0.0
        self._latest = None
        self._out_times = []
        self._viewers = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # -- viewers -------------------------------------------------------------

    def attach(self):
        with self._lock:
            self._viewers += 1
            if self._viewers == 1:
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, name="live-stack", daemon=True)
                self._thread.start()

    def detach(self):
        with self._lock:
            self._viewers = max(0, self._viewers - 1)
            if self._viewers == 0:
                self._stop.set()

    def latest(self):
        return self._latest

    def fps(self):
        now = time.time()
        self._out_times = [t for t in self._out_times if now - t <= 5.0]
        return round(len(self._out_times) / 5.0, 1)

    def accumulated(self):
        """The running mean/EMA, in the same 0-255 units as a decoded frame."""
        return self._acc

    def depth(self):
        return self._depth

    # -- the pipeline --------------------------------------------------------

    def step(self, frame, now=None):
        now = time.time() if now is None else now
        arr = _decode_scaled(frame.data)
        if (self._acc is None or self._acc.shape != arr.shape
                or self._last_frame_ts is None or frame.ts - self._last_frame_ts > GAP_RESET_S):
            self._acc = np.zeros_like(arr)
            self._t = 0
            self._params = None
        self._last_frame_ts = frame.ts
        self._t += 1
        a = max(1.0 / self._t, 1.0 / max(self._depth, 1))
        self._acc += (arr - self._acc) * np.float32(a)
        img = self.accumulated()
        if self._params is None or now - self._params_at >= REFRESH_S:
            self.refresh_params(fps=max(self.fps(), 1.0), img=img)
            self._params_at = now
        jpeg = render.to_jpeg(self.tone(img), quality=JPEG_QUALITY)
        self._out_times.append(now)
        self._latest = Frame(jpeg, now)
        return jpeg

    def refresh_params(self, fps, img=None):
        """Estimate tone parameters and depth from a sixteenth of the pixels.

        `render.calibration` on the full frame measured 15 ms, which the 60 ms
        per-frame budget does not contain; on the subsample it is under 2 ms and
        the black level is a percentile over 30k pixels a channel, which is
        plenty.
        """
        img = self.accumulated() if img is None else img
        if img is None:
            return
        sub = img[::SUBSAMPLE, ::SUBSAMPLE]
        bl, gains = render.calibration(sub, self.s)
        lin = np.clip(sub - bl, 0, None) * gains
        L = lin.mean(axis=2)
        lavg = float(np.exp(np.mean(np.log(1e-3 + L))))
        lw = float(np.percentile(self.s.night_key / lavg * L, 99.9))
        self._params = (np.asarray(bl, np.float32), np.asarray(gains, np.float32), lavg, lw)
        subject = float(np.percentile(L, self.s.subject_percentile))
        # previous_depth=None on purpose: choose_depth's ramp derives alpha from
        # settings.interval_s, i.e. it assumes 600 s between calls. At this
        # cadence it would jump half the remaining distance every two seconds,
        # which is not smoothing. The moving average is already smooth in `a`.
        self._depth = stack.choose_depth(
            subject, self.s, fps, None,
            hard_cap=max(1, int(self.s.live_max_window_s * fps)))

    def tone(self, img):
        """`render._tone_natural`'s arithmetic in float32, with no chroma blur
        and no NLM: the moving average is the denoiser."""
        bl, gains, lavg, lw = self._params
        x = img - bl
        np.maximum(x, 0, out=x)
        x *= gains
        L = x.mean(axis=2)
        Ls = L * np.float32(self.s.night_key / lavg)
        # max(lw, ...): a perfectly flat frame (only seen in tests) has lw == 0,
        # and render._tone_natural guards the same division the same way.
        Ld = Ls * (1 + Ls / np.float32(max(lw, 1e-6) ** 2)) / (1 + Ls)
        x *= (Ld / np.maximum(L, np.float32(1e-6)))[..., None]
        # Clip before the gamma: a fractional power of a negative number is NaN.
        np.clip(x, 0, 1, out=x)
        rgb = cv2.pow(x, GAMMA)
        Y = rgb.mean(axis=2, keepdims=True)
        # Darker tones desaturate, as in the stills.
        out = Y + (rgb - Y) * np.clip(Y / np.float32(0.4), 0.4, 1.0)
        return (np.clip(out, 0, 1) * 255).astype(np.uint8)

    # -- thread --------------------------------------------------------------

    def _run(self):
        last_ts = None
        while not self._stop.is_set():
            frame = self.buffer.latest()
            if frame is not None and frame.ts != last_ts:
                last_ts = frame.ts
                try:
                    self.step(frame)
                except Exception:
                    # A viewer's feed is never worth taking the process down for.
                    log.exception("live stack step failed")
            else:
                time.sleep(POLL_S)
        self._acc = None
        self._latest = None
