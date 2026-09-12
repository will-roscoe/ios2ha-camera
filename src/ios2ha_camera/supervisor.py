"""Wires ingest, measurement, stacking, rendering, archiving and MQTT together.

Threading matters here, so it is stated once:

- The capture loop and the health loop each run on their own plain thread.
  Health is on a short tick of its own because a failure that only surfaced
  with the next still would take up to the capture interval to appear, and the
  point of this service is noticing failure in minutes.
- Device actions arriving over MQTT run on a single worker thread, never on
  paho's network thread. paho re-raises exceptions out of its callbacks, so one
  ONVIF timeout there would drop the connection and leave the supervisor deaf
  to every later control; and a night stack takes tens of seconds, long enough
  to miss the broker keepalive.
- Nothing here runs on the aiohttp event loop, which serves the live view only.
  PhoneManager's calls block and raise if called from a running loop.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from . import measure, render, stack
from .phone import PhoneUnavailable
from .settings import CONTROLS

log = logging.getLogger(__name__)


def estimate_fps(buffer, n=30, default=12.0):
    """Frame rate from the arrival times of the newest n frames."""
    recent = buffer.recent(n)
    if len(recent) < 2:
        return default
    span = recent[-1].ts - recent[0].ts
    return (len(recent) - 1) / span if span > 0 else default


def _dark_noise(rgb):
    """Noise and signal-to-noise in the darkest tenth of the finished still.

    Measured as the spread of a high-pass residual, so scene texture in the
    dark region is not counted as noise, and with a median absolute deviation
    so a few bright pixels cannot set it. The plain standard deviation of the
    whole image, which this replaces, rises with scene detail and would read as
    the camera getting noisier whenever something interesting appeared.
    """
    lum = rgb.astype(np.float32).mean(axis=2)
    resid = lum - cv2.GaussianBlur(lum, (0, 0), 2.0)
    dark = lum <= np.percentile(lum, 10)
    if dark.sum() < 16:
        return 0.0, None
    r = resid[dark]
    noise = float(1.4826 * np.median(np.abs(r - np.median(r))))
    signal = float(lum[dark].mean())
    return round(noise, 3), (round(signal / noise, 2) if noise > 1e-6 else None)


class Supervisor:
    def __init__(self, settings, buffer, mqtt, archive, onvif=None, phone=None, *,
                 probe_n=15, fresh_s=30.0, min_free_bytes=2 * 2**30, device_executor=None):
        self.s = settings
        self.buffer = buffer
        self.mqtt = mqtt
        self.archive = archive
        self.onvif = onvif
        self.phone = phone
        self.probe_n = probe_n
        self.fresh_s = fresh_s
        self.min_free_bytes = min_free_bytes
        self.stream_state = None
        self.capture_requested = threading.Event()
        self.last_stack = None
        self._previous_depth = None
        self._last_state = {}
        self._last_jpeg = None
        self._device_executor = device_executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="device")

    # -- publishing ---------------------------------------------------------

    def publish_state(self, d):
        """Publish and remember, so a reconnect can replay the current values."""
        if not d:
            return
        self._last_state.update(d)
        self.mqtt.publish_state(d)

    def _event(self, d):
        self.mqtt.publish_event(d)
        return d

    def publish_controls(self):
        """Republish every control, the last measurements and the last still.

        Called on each MQTT connect. Without it, Home Assistant shows every
        control as unknown until someone changes it, and a broker restart drops
        the retained values.
        """
        d = {n: getattr(self.s, n) for n in sorted(CONTROLS) if n != "black_level_override"}
        if self.s.black_level_override is not None:
            d.update({f"black_level_{i}": v for i, v in enumerate(self.s.black_level_override)})
        self.mqtt.publish_state(d)
        self._last_state.update(d)
        if self._last_state:
            self.mqtt.publish_state(self._last_state)
        if self._last_jpeg is not None:
            self.mqtt.publish_still(self._last_jpeg)

    # -- capture ------------------------------------------------------------

    def _fresh(self, frames, now, max_age):
        return [f for f in frames if now - f.ts <= max_age]

    def produce_still(self, now_fps, now=None):
        now = time.time() if now is None else now
        t0 = time.monotonic()

        probe_frames = self._fresh(self.buffer.recent(self.probe_n), now, self.fresh_s)
        if not probe_frames:
            # Stacking whatever is left in the buffer would archive a picture of
            # the past as if it were now.
            return self._event({"ts": now, "error": "no fresh frames"})
        probe = [stack.decode(f) for f in probe_frames]
        mean = np.mean(probe, axis=0)

        bl_probe = measure.black_level(mean)
        pct = measure.luminance_percentiles(mean, bl_probe)
        subject = measure.luminance_percentiles(
            mean, bl_probe, (self.s.subject_percentile,))[self.s.subject_percentile]

        depth = stack.choose_depth(subject, self.s, now_fps, self._previous_depth)
        self._previous_depth = depth
        required = measure.required_depth(subject, self.s.target_level, self.s.clean_factor)

        window = self._fresh(self.buffer.recent(depth), now, self.s.window_max_s)
        if not window:
            return self._event({"ts": now, "error": "no frames in window"})
        used = len(window)

        stacked = stack.stack_frames(window)
        self.last_stack = stacked
        black, gains = render.calibration(stacked, self.s)
        rgb = render.render(stacked, self.s, applied=(black, gains))
        jpeg = render.to_jpeg(rgb)
        self._last_jpeg = jpeg
        self.mqtt.publish_still(jpeg)

        free = self.archive.free_bytes()
        path = None
        if free >= self.min_free_bytes:
            path = self.archive.write_still(jpeg, now)
        else:
            log.warning("archive skipped: %.1f GB free is under the %.1f GB floor",
                        free / 2**30, self.min_free_bytes / 2**30)

        noise, snr = _dark_noise(rgb)
        # Dynamic range is measured before black-level subtraction: afterwards
        # the 0.5th percentile is zero by construction, since the black level
        # is itself the 0.5th percentile.
        raw = measure.luminance_percentiles(mean, np.zeros(3), (0.5, 99.5))

        self.publish_state({
            "depth_used": used,
            "depth_required": required,
            "subject_level": round(subject, 2),
            "capture_duration": round(time.monotonic() - t0, 2),
            "colour_temperature": round(measure.colour_temperature_k(mean)),
            "lum_mean": round(float(measure.luminance(mean, bl_probe).mean()), 2),
            **{f"lum_p{int(p)}": round(pct[p], 2) for p in (1, 10, 50, 90, 99)},
            "dynamic_range": round(raw[99.5] / max(raw[0.5], 1.0), 1),
            "highlight_clip": round(float((stacked.max(axis=2) >= 250).mean()), 4),
            "noise": noise,
            "snr": snr,
            "motion": (round(measure.motion_level(probe[-2], probe[-1]), 3)
                       if len(probe) >= 2 else 0.0),
            "focus_sharpness": round(measure.focus_sharpness(stacked), 1),
            "frame_rate": round(now_fps, 1),
            "effective_exposure_required": round(required / max(now_fps, 1e-6), 2),
            "effective_exposure_achieved": round(used / max(now_fps, 1e-6), 2),
            "archive_free_gb": round(free / 2**30, 1),
            **{f"wb_gain_{c}": round(float(g), 3) for c, g in zip("rgb", gains)},
            **{f"black_level_applied_{c}": round(float(b), 3) for c, b in zip("rgb", black)},
        })

        event = {"ts": now, "depth_used": used, "depth_requested": depth,
                 "short": used < depth, "subject_level": round(subject, 2),
                 "path": path, "archived": path is not None}
        if path is None:
            event["archive_skipped"] = "low_disk"
        return self._event(event)

    # -- controls -----------------------------------------------------------

    def handle_control(self, name, value):
        """Apply one Home Assistant control write.

        Never raises: paho tears the network loop down on a callback exception,
        and losing control of the camera because one ONVIF call timed out would
        be a far worse failure than the one being reported.
        """
        try:
            if name == "capture_now":
                # Only ask. Stacking here would run on the MQTT thread.
                self.capture_requested.set()
            elif name == "screenshot":
                self._submit(name, self._screenshot)
            elif name == "relaunch_app":
                self._submit(name, self._relaunch)
            elif name == "autofocus":
                self._submit(name, lambda: self._autofocus(bool(value)))
            elif self.s.apply_control(name, value):
                self.publish_state({name: getattr(self.s, name, value)})
            else:
                log.warning("ignoring unknown control %r", name)
        except Exception:
            log.exception("control %r failed", name)

    def _submit(self, name, fn):
        self._device_executor.submit(self._run_device_action, name, fn)

    def _run_device_action(self, name, fn):
        try:
            fn()
        except Exception as e:
            log.exception("device action %r failed", name)
            self._event({"ts": time.time(), "control": name,
                         "error": f"{type(e).__name__}: {e}"})

    def _screenshot(self):
        if self.phone is None:
            raise RuntimeError("phone management is not configured")
        self.mqtt.publish_screenshot(self.phone.screenshot())

    def _relaunch(self):
        if self.phone is None:
            raise RuntimeError("phone management is not configured")
        self.phone.launch()
        # Deliberately an event, not a state: relaunching leaves streaming off
        # until someone taps Start streaming in the app.
        self._event({"ts": time.time(), "control": "relaunch_app", "result": "launched"})

    def _autofocus(self, on):
        try:
            if self.onvif is not None and not self.onvif.set_autofocus("AUTO" if on else "MANUAL"):
                raise RuntimeError("the camera rejected the autofocus change")
        except Exception:
            # Home Assistant shows a switch as flipped as soon as it sends the
            # command, so the real state has to be pushed back.
            self.publish_state({"autofocus": self.s.autofocus})
            raise
        self.s.apply_control("autofocus", on)
        self.publish_state({"autofocus": self.s.autofocus})

    # -- health -------------------------------------------------------------

    def on_stream_state(self, state):
        """Ingest calls this on every change; published at once, not at the
        next interval, so an outage is visible within the health tick."""
        self.stream_state = state
        self.publish_state({"stream_state": state})

    def health_state(self, now=None):
        now = time.time() if now is None else now
        newest = self.buffer.newest_ts()
        age = None if newest is None else round(now - newest, 1)
        d = {
            "last_frame_age": age,
            "camera_online": (self.stream_state == "streaming"
                              and age is not None and age < self.fresh_s),
            "frame_rate": round(estimate_fps(self.buffer), 1),
        }
        if self.stream_state is not None:
            d["stream_state"] = self.stream_state
        return d

    def phone_state(self):
        """Battery figures, or nothing when the cable is out.

        Only PhoneUnavailable is swallowed. PhoneManagerMisuseError is a wiring
        bug and must stay loud rather than looking like an unplugged phone.
        """
        if self.phone is None:
            return {}
        try:
            b = self.phone.battery()
        except PhoneUnavailable as e:
            log.info("battery unavailable: %s", e)
            return {}
        return {"battery": b.get("level"), "battery_temperature": b.get("temperature"),
                "charging": bool(b.get("charging"))}

    # -- loops --------------------------------------------------------------

    def run_capture(self, stop):
        # A first still shortly after start, rather than a whole interval of
        # nothing, but late enough for the buffer to fill.
        next_at = time.monotonic() + min(self.s.interval_s, 30)
        while not stop.is_set():
            manual = self.capture_requested.wait(max(0.0, next_at - time.monotonic()))
            if stop.is_set():
                break
            self.capture_requested.clear()
            try:
                self.produce_still(estimate_fps(self.buffer))
            except Exception:
                log.exception("capture failed")
            if not manual:
                # A manual capture must not shift the schedule.
                next_at += self.s.interval_s
            if next_at <= time.monotonic():
                next_at = time.monotonic() + self.s.interval_s

    def run_health(self, stop, every=30.0, battery_every=300.0):
        last_battery = 0.0
        while not stop.is_set():
            try:
                self.publish_state(self.health_state())
                if time.monotonic() - last_battery >= battery_every:
                    last_battery = time.monotonic()
                    self.publish_state(self.phone_state())
            except Exception:
                log.exception("health tick failed")
            stop.wait(every)

    def close(self):
        self._device_executor.shutdown(wait=False)
