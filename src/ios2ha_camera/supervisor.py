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

import contextlib
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import cv2
import numpy as np

from . import focus, horizon, measure, render, stack
from .backgrounding import BackgroundingPolicy
from .phone import PhoneUnavailable
from .reboot import WEDGE_FOR_S
from .settings import BLACK_LEVEL_CHANNELS, CONTROLS, MAX_ROTATION, _coerce_bool
from .states import ACTIVE_STATES
from .upstream import CAPTURE_OPEN_TIMEOUT_S

# A phone screen is mostly text, which is where JPEG artefacts show worst, so
# this sits above the 85 the live stack uses. On a 2436x1125 screenshot it turns
# ~9.5 MB of PNG into ~260 KB.
SCREENSHOT_JPEG_QUALITY = 90

# A lens reading this close to the target is already there: GetStatus reports in
# 1/255 steps (0.682352901 is 174/255), so an exact comparison would move a lens
# that is already where it was asked to be.
FOCUS_TOLERANCE = 0.005
# Tries at the phone's focus before giving up until the next session. The app
# needs up to a second after being foregrounded before it answers again
# (measured: the stream resumes 0.9-1.1 s after the foreground call returns).
FOCUS_ATTEMPTS = 3
# How far the lens is nudged away before being driven to its held position.
FOCUS_NUDGE = 0.03
FOCUS_RETRY_S = 1.0

# A flat field is gathered like a still, but at a fixed depth: the grain it records
# is at the level of the stills, so it needs about as many frames to rise above
# the frame-to-frame noise. 600 frames is ~45 s at the phone's 13.9 fps.
FLAT_FRAMES = 600
FLAT_WINDOW_S = 90.0
FLAT_MIN_FRAMES = 100
# A focus probe: let the lens settle, then average a few seconds of frames -- a
# single night frame is too dark to judge edges on. Measured 2026-09-28: ~40-frame
# averages gave clean curves where single frames did not.
FOCUS_FRAMES = 35
FOCUS_MIN_FRAMES = 10
FOCUS_SETTLE_S = 0.5
FOCUS_PROBE_TIMEOUT_S = 8.0
# The dusk run waits for the scene to start needing brightening (night weight above
# 0) and not to be dark yet (below this), so it has light to judge edges by and a
# restart in the night never triggers it.
DUSK_MAX_WEIGHT = 0.5
DUSK_TRIES = 3
# Level horizon refuses a still made with more of the night render than this.
# Measured 28 Sep: at night, reflections in the glass misled it by 1.4 degrees;
# daytime stills measured only just above the default target level, so an overcast
# noon has a little night weight and must not be refused as night.
LEVEL_MAX_WEIGHT = 0.5

# How long the app stays in the background when restarting its capture session.
WB_RESET_PAUSE_S = 5.0

log = logging.getLogger(__name__)


def estimate_fps(buffer, n=30, default=12.0, since=None):
    """Frame rate from the arrival times of the newest n frames.

    `since` excludes frames from before the current session: with on-demand the
    buffer also holds probe frames a minute apart, and averaging across them
    would report 0.02 fps for a stream running at 13.9.
    """
    recent = [f for f in buffer.recent(n) if since is None or f.ts >= since]
    if len(recent) < 2:
        return default
    span = recent[-1].ts - recent[0].ts
    return (len(recent) - 1) / span if span > 0 else default


# -- cable self-heal ---------------------------------------------------------
#
# The container bind-mounts usbmuxd's socket as a file. usbmuxd exits whenever no
# device is attached and creates a new socket on restart, so the container is left
# holding a deleted inode and every cable operation fails until it is recreated.
#
# The host-side fix is the systemd drop-in installed by
# scripts/install-usbmuxd-hook.sh, which restarts the container the moment usbmuxd
# comes back. That is the primary mechanism and it acts in seconds. What follows is
# the fallback for hosts where the hook is not installed, so it is deliberately slow
# and heavily guarded: a service that restarts itself on a wrong signal is worse
# than the fault it is trying to fix.

# A cable already broken at boot must not restart the container forever: every new
# process would find the same dead socket. Waiting out an interval longer than the
# fault takes to appear means a genuine orphaning is always seen by a process that
# has been up a while.
CABLE_MIN_UPTIME_S = 600.0
# The failure has to persist. usbmuxd restarting normally is brief.
CABLE_FAILING_FOR_S = 300.0
# Asking to exit is not instant, and the health tick keeps running meanwhile.
CABLE_RESTART_COOLDOWN_S = 3600.0

# usbmuxd itself is unreachable. NOT the same as the phone being unplugged, which
# raises NoDeviceConnectedError or DeviceNotFoundError: usbmuxd is answering fine
# then, and restarting would fire every time the cable is pulled.
_USBMUXD_DOWN = ("ConnectionFailedToUsbmuxdError",)


def is_usbmuxd_down(exc):
    """True when `exc` means usbmuxd is unreachable, not that no phone is attached.

    PhoneManager._call wraps everything as PhoneUnavailable but keeps the original
    on __cause__ and its class name in the message, so both are checked. The class
    is authoritative; the message is the fallback for anything that arrives
    unwrapped.
    """
    seen = set()
    e = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if type(e).__name__ in _USBMUXD_DOWN:
            return True
        e = e.__cause__ or e.__context__
    # Deliberately the full class name rather than "usbmuxd": an unrelated message
    # that merely mentions the daemon must not trigger a restart.
    return any(n.lower() in str(exc).lower() for n in _USBMUXD_DOWN)


def _terminate_self():
    """Ask this process to exit, and let Docker's restart policy bring it back.

    SIGTERM rather than os._exit so the normal shutdown path runs: the offline
    availability gets published, so Home Assistant shows the camera as unavailable
    instead of holding a stale state until the new process starts.
    """
    log.error("restarting: usbmuxd has been unreachable long enough that the socket "
              "mount is presumed orphaned. Install scripts/install-usbmuxd-hook.sh "
              "on the host to have this handled in seconds instead.")
    os.kill(os.getpid(), signal.SIGTERM)


class _FocusRejected(Exception):
    """The phone answered and refused: retrying at once would be refused again."""


class _NoFocusFrames(Exception):
    """A focus probe gathered too few frames to judge."""


def _curve_text(curve):
    return ", ".join(f"{p:.3f}:{v:.3g}" for p, v in sorted(curve.items(), reverse=True))


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
                 upstream=None, probe_n=15, fresh_s=30.0, min_free_bytes=2 * 2**30,
                 device_executor=None, request_restart=None, clock=None,
                 starter=None, keyboard=None, opener=None, rebooter=None,
                 emergency=None, store=None, sleep=time.sleep, flat=None, colour=None):
        # Injected so tests can drive time and record the restart instead of
        # signalling the test runner's own process.
        self._clock = clock or time.monotonic
        self._request_restart = request_restart or _terminate_self
        self._started_at = self._clock()
        self._cable_down_since = None
        self._last_cable_restart = None
        self._cable_state = "ok"
        # None until the cable has been asked once, and published as unknown
        # rather than guessed. Refreshed on the battery cadence rather than the
        # health tick: it is a usbmux round trip, and asking every 30 s would
        # spend the cable on a question whose answer changes rarely.
        self._app_running = None
        # Device actions queued or running, by dedup key, and the one follow-up
        # a latest-wins action has waiting; see _submit.
        self._device_lock = threading.Lock()
        self._device_busy = set()
        self._device_again = {}
        self.s = settings
        # Where control values are kept across restarts; None keeps them in
        # memory only, which is what every test that does not ask for it gets.
        self.store = store
        self._sleep = sleep
        # The sensor's recorded fixed grain, or None when the feature is off.
        self.flat = flat
        self._flat_state = "ready" if getattr(flat, "ready", False) else "none"
        self._flat_requested = False
        # The colour a picked region is held to at night, or None when off.
        self.colour = colour
        self._colour_state = "ready" if getattr(colour, "ready", False) else "none"
        # The last still as it was rendered: (linear stack, black level, night
        # weight, white-balance gains), set together once it is made, so Set colour
        # reference on another thread never pairs one still's stack with another's
        # weight, and records the colour a person saw rather than the stack under it.
        # The stack is flat-corrected and levelled: the region is drawn on the still.
        self._last_render = None
        self._horizon_state = "none"
        # The last still's JPEG and night weight, set together so the Level horizon
        # button, on another thread, never pairs one still with another's weight.
        self._last_shown = None
        # The service's own focus runs; see run_focus.
        self._focus_requested = False
        # The lens has one owner at a time: a focus run holds this for its whole
        # sweep, and applying held focus skips rather than wait for it.
        self._lens_lock = threading.Lock()
        self._focus_state = "idle"
        # The last still's night weight, which the dusk run is decided on; None
        # when the last still failed, so a failure never looks like dusk.
        self._last_weight = None
        # The one before it, from the last still that had one: dusk is the light
        # falling, which takes two readings to see.
        self._prev_weight = None
        self._good_weight = None
        self._dusk_day = None
        self._dusk_tries = 0
        self._dusk_done = False
        # Reads the switch and both thresholds live, because all three are Home
        # Assistant controls that can change between one capture and the next.
        self.backgrounding = BackgroundingPolicy(
            phone,
            lambda: bool(getattr(self.s, "backgrounding", False)),
            lambda: float(getattr(self.s, "background_below_pct", 50.0)),
            lambda: float(getattr(self.s, "resume_above_pct", 80.0)),
            # Used only when the developer call above it has already failed;
            # see emergency.py for why it is never the first choice.
            emergency=emergency,
        )
        self.emergency = emergency
        self.buffer = buffer
        self.mqtt = mqtt
        self.archive = archive
        self.onvif = onvif
        self.phone = phone
        self.upstream = upstream
        self.probe_n = probe_n
        self.fresh_s = fresh_s
        self.min_free_bytes = min_free_bytes
        self.stream_state = None
        self.capture_requested = threading.Event()
        self.last_stack = None
        self._previous_depth = None
        # Written from the capture, health, device and MQTT network threads, and
        # read whole on reconnect, so every access goes through the lock.
        self._last_state = {}
        # Guards the dict alone, and is only ever held for a copy or an update:
        # it is taken on the event loop by every reader of the current state.
        self._state_lock = threading.Lock()
        # Held across a whole publish -- store, MQTT, listeners -- so the order
        # values are stored in and the order listeners hear them in are the
        # same order. See publish_state.
        self._publish_lock = threading.Lock()
        # Appended to during startup and never removed; see add_state_listener.
        self._state_listeners = []
        self._last_jpeg = None
        self._last_screenshot_jpeg = None
        # Both optional: without them the supervisor behaves exactly as before,
        # which is what happens when the D-Bus proxy or BlueZ is missing.
        self.starter = starter
        self.keyboard = keyboard
        self.opener = opener
        self.rebooter = rebooter
        self._device_executor = device_executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="device")

    @property
    def last_screenshot_jpeg(self):
        """The most recent phone screenshot, or None before the first press.

        Read by the live server's /api/v1/snapshot/screenshot, so the camera
        entity shows the same image the retained MQTT topic carries, without
        touching the phone.
        """
        return self._last_screenshot_jpeg

    @property
    def last_still_jpeg(self):
        """The most recent stacked still, or None before the first one.

        Read by the live server's /still.jpg so a camera thumbnail shows the
        stacked image rather than a single raw frame, without touching the phone.
        """
        return self._last_jpeg

    # -- publishing ---------------------------------------------------------

    def publish_state(self, d):
        """Publish and remember, so a reconnect can replay the current values.

        Called from the capture, health and device threads and from paho's
        network thread, so two calls can carry the same key at once. The whole
        sequence runs under `_publish_lock` for that reason: without it, one
        thread can store its value, stall in the MQTT publish, and tell the
        listeners only after a second thread has stored and announced a newer
        one. The store then holds the newer value and every listener the older,
        and nothing corrects that until the key is published again -- which,
        for a setting nobody touches and with no broker reconnect to replay it,
        may be never.
        """
        if not d:
            return
        with self._publish_lock:
            with self._state_lock:
                self._last_state.update(d)
            self.mqtt.publish_state(d)
            # Told last, and never before the values above are stored: a
            # listener may be asked for the current state the instant it hears
            # a change -- by a client of its own, on a thread of its own -- and
            # told first it would answer with the value it has just been told
            # replaced. `_state_lock` is not held here, so answering that
            # question never waits on a listener.
            for listener in self._state_listeners:
                try:
                    listener(d)
                except Exception:
                    # Some of these calls arrive on paho's network thread, where
                    # an exception does not merely lose one event: it tears down
                    # the connection and leaves the camera deaf to every later
                    # control.
                    log.exception("state listener failed")

    def add_state_listener(self, fn):
        """Also hand every published state to `fn`, as a plain dict.

        For an interface alongside MQTT that must not be able to disagree with
        it: both are fed from this one call, so there is a single order of
        events rather than two that can drift.

        `fn` is called with the publish lock held, so every publisher in the
        process waits while it runs. It must therefore return promptly without
        blocking -- hand the work to another thread or loop, as
        `EventHub.publish` does -- and must not call `publish_state` itself,
        which would deadlock. Reading `current_state()` from it is safe. An
        exception from it is logged and costs only that listener's event.

        Listeners are added during startup and never removed, which is why the
        list needs no lock. Appending cannot disturb a publish already walking
        it, and a publish that races the registration simply does not reach the
        new listener -- which costs nothing, because a listener added a moment
        ago has no client it could already have told anything.
        """
        self._state_listeners.append(fn)

    def current_state(self):
        """A copy of the values last published, for an interface that is asked
        for state rather than told it.

        A copy, taken under the lock, because the capture, health, device and
        MQTT network threads all write these while a reader is reading them.

        Deliberately not `health_state()`: that measures, which takes time and
        may reach the phone, and its caller is the event loop serving the live
        view, which nothing may block. It is also no fresher in practice -- the
        health loop publishes everything it measures on its own tick, so this
        holds exactly what the broker holds.
        """
        with self._state_lock:
            return dict(self._last_state)

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
        # Always, not only while an override is set. The three numbers declare a
        # state key, and an entity whose key is usually absent reads as three
        # blank boxes in Home Assistant with no way to tell "no override" from
        # "this service is broken". `black_level_override_on` is what carries
        # that distinction now, so the values themselves can always be published.
        d.update({f"black_level_{i}": v for i, v in enumerate(self.s.black_level)})
        self.publish_state(d)
        # A snapshot, never the live dict: this runs on the MQTT network thread
        # while the capture and health threads are still writing, and iterating
        # the dict itself would raise "changed size during iteration" straight
        # into a caller that only logs it -- silently losing the republish.
        with self._state_lock:
            snapshot = dict(self._last_state)
        if snapshot:
            # Straight to the client rather than through publish_state: this
            # replay exists because a broker restart drops retained values, and
            # anything else listening never lost them, so it would be handed a
            # long list of things that have not changed.
            self.mqtt.publish_state(snapshot)
        if self._last_jpeg is not None:
            self.mqtt.publish_still(self._last_jpeg)

    # -- capture ------------------------------------------------------------

    def _fresh(self, frames, now, max_age):
        return [f for f in frames if now - f.ts <= max_age]

    def produce_still(self, now_fps=None, now=None):
        """Make, publish and archive one still. Reports failure over MQTT.

        A stacking, render or archive error would otherwise leave every
        measurement stale while the stream itself still reported healthy, which
        is the silent failure this service exists to end.
        """
        try:
            event = self._produce_still(now_fps, now)
        except Exception as e:
            log.exception("capture failed")
            return self._event({"ts": time.time() if now is None else now,
                                "error": f"{type(e).__name__}: {e}"})
        # The event only reaches MQTT, and with MQTT off it reaches nobody: on
        # 2026-09-28 a capture that found no frames left the log silent for as
        # long as the camera stayed dark.
        if event.get("error"):
            log.warning("still failed: %s", event["error"])
        return event

    def _produce_still(self, now_fps=None, now=None):
        now = time.time() if now is None else now
        self._last_weight = None
        t0 = time.monotonic()

        # The `with` block is load-bearing: produce_still() swallows every
        # exception below, so a manual release would be skipped by a decode
        # error and the phone would stay connected until the container
        # restarted -- exactly the drain this feature exists to end.
        hold = (self.upstream.hold("capture") if self.upstream is not None
                else contextlib.nullcontext())
        with hold:
            if self.upstream is not None:
                # The hold only wakes the upstream thread, so a session it opens
                # starts after `now` and has no start yet; its frames clear a
                # floor of `now`, and older ones (a probe's) must not. A session
                # already open keeps its earlier start, so a capture while
                # someone is viewing uses the frames already buffered at once.
                floor = max(self.upstream.session_start_ts() or now, now - self.s.window_max_s)
                self.upstream.wait_for_frames(self.probe_n, floor, CAPTURE_OPEN_TIMEOUT_S)
                probe_frames = [f for f in self.buffer.recent(self.probe_n) if f.ts >= floor]
                if not probe_frames:
                    return self._event(
                        {"ts": now, "error": f"no frames: {self.upstream.state(now)}"})
            else:
                probe_frames = self._fresh(self.buffer.recent(self.probe_n), now, self.fresh_s)
                if not probe_frames:
                    # Stacking whatever is left in the buffer would archive a picture of
                    # the past as if it were now.
                    return self._event({"ts": now, "error": "no fresh frames"})
            probe = [stack.decode(f) for f in probe_frames]
            # np.mean(probe, axis=0) first stacks every decoded frame into one
            # (N,H,W,3) array before reducing -- ~373MB for probe_n=15 at
            # 1920x1080 float32, thrown away immediately, and this whole block
            # runs inside the `with hold:` above, so that cost is added straight
            # to how long the phone connection stays open. Measured
            # bit-identical to np.mean for stack.decode's float32 frames, at
            # 73-167ms against np.mean's 200ms-1.1s.
            mean = sum(probe) / len(probe)

            bl_probe = measure.black_level(mean)
            pct = measure.luminance_percentiles(mean, bl_probe)
            subject = measure.luminance_percentiles(
                mean, bl_probe, (self.s.subject_percentile,))[self.s.subject_percentile]

            if now_fps is None:
                if self.upstream is not None:
                    # Measured from this session's frames, never the whole buffer: the
                    # probe frames in there are a minute apart.
                    now_fps = estimate_fps(self.buffer, since=floor)
                else:
                    now_fps = estimate_fps(self.buffer)
            depth = stack.choose_depth(subject, self.s, now_fps, self._previous_depth)
            self._previous_depth = depth
            required = measure.required_depth(subject, self.s.target_level, self.s.clean_factor)

            if self.upstream is not None:
                self.upstream.wait_for_frames(depth, floor, self.s.window_max_s)
                # A list, not a generator: stack_frames iterates its source three
                # times (one unclipped pass, two clipping passes).
                window = [f for f in self.buffer.recent(depth) if f.ts >= floor]
            else:
                window = self._fresh(self.buffer.recent(depth), now, self.s.window_max_s)
            if not window:
                return self._event({"ts": now, "error": "no frames in window"})
            used = len(window)

        stacked = stack.stack_frames(window)
        self.last_stack = stacked
        black, gains = render.calibration(stacked, self.s)
        # The day/night blend rides on the same subject measurement that
        # already drives depth -- one number, two consumers, so they cannot
        # disagree about how much brightening the scene needs.
        weight = render.night_weight(subject, self.s.target_level)
        self._prev_weight, self._good_weight = self._good_weight, weight
        self._last_weight = weight
        # Before the black level is read, so it is measured on the corrected
        # stack; the pattern has no mean of its own, so it barely moves it.
        scales, smoothing = None, 1.0
        if self.flat is not None and self.s.flat_field_correction:
            stacked, scales, smoothing = self.flat.correct(stacked, weight)
            if scales is not None:
                black, gains = render.calibration(stacked, self.s)
        # Levelled after the flat field, which is fixed to the sensor's pixels, and
        # before anything measured on the view, whose regions are drawn on the still.
        # The black level, gains and the stack's own readings stay those of the
        # unturned stack: resampling narrows its noise and softens it a little, and
        # setting a rotation must not look like the black level or the focus moving.
        rotation = self.s.rotation            # once: the still is paired with it
        unturned = stacked
        if rotation:
            stacked = horizon.level(stacked, rotation)
        gains = self._colour_gains(stacked, black, gains, weight)
        rgb = render.render(stacked, self.s, applied=(black, gains), night_weight=weight,
                            smoothing=smoothing)
        self._last_render = (stacked, black, weight, np.asarray(gains, dtype=np.float64))
        jpeg = render.to_jpeg(rgb)
        self._last_jpeg = jpeg
        self._last_shown = (jpeg, weight, rotation)
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
            "last_still": datetime.fromtimestamp(now, tz=UTC).isoformat(),
            "flat_field_scale": (round(sum(scales) / len(scales), 2)
                                 if scales is not None else 0.0),
            "depth_used": used,
            "depth_required": required,
            "subject_level": round(subject, 2),
            "capture_duration": round(time.monotonic() - t0, 2),
            "colour_temperature": round(measure.colour_temperature_k(mean)),
            "lum_mean": round(float(measure.luminance(mean, bl_probe).mean()), 2),
            **{f"lum_p{int(p)}": round(pct[p], 2) for p in (1, 10, 50, 90, 99)},
            "dynamic_range": round(raw[99.5] / max(raw[0.5], 1.0), 1),
            # Per cent, to match the sensor's declared unit.
            "highlight_clip": round(float((unturned.max(axis=2) >= 250).mean()) * 100, 2),
            "noise": noise,
            "snr": snr,
            "motion": (round(measure.motion_level(probe[-2], probe[-1]), 3)
                       if len(probe) >= 2 else 0.0),
            "focus_sharpness": round(measure.focus_sharpness(unturned), 1),
            "frame_rate": round(now_fps, 1),
            "effective_exposure_required": round(required / max(now_fps, 1e-6), 2),
            "effective_exposure_achieved": round(used / max(now_fps, 1e-6), 2),
            "archive_free_gb": round(free / 2**30, 1),
            **{f"wb_gain_{c}": round(float(g), 3)
               for c, g in zip("rgb", gains, strict=True)},
            **{f"black_level_applied_{c}": round(float(b), 3)
               for c, b in zip("rgb", black, strict=True)},
        })

        event = {"ts": now, "depth_used": used, "depth_requested": depth,
                 "short": used < depth, "subject_level": round(subject, 2),
                 "path": path, "archived": path is not None}
        if path is None:
            event["archive_skipped"] = "low_disk"
        # One line per still. Without it the log goes silent for ten minutes at
        # a time and there is no way to see when the last good still was, which
        # is the first thing anyone asks when the timelapse has a gap.
        log.info("still: depth %d of %d, subject %.1f, %.1f s, %s",
                 used, depth, subject, time.monotonic() - t0,
                 f"archived {path}" if path else "NOT archived (low disk)")
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
            elif name == "prepare_phone":
                self._submit(name, self._prepare_phone)
            elif name == "start_streaming":
                if self.starter is None:
                    raise RuntimeError("the keyboard is not configured")
                # manual=True: a person asking overrides the pause switch and
                # the rate limit, though never the guards around the keys.
                self._submit(name, lambda: self.starter.attempt(True))
            elif name == "set_colour_reference":
                if self.colour is None:
                    raise RuntimeError("the colour reference is switched off "
                                       "(COLOUR_REFERENCE_FILE is empty)")
                self._submit(name, self._set_colour_reference)
            elif name == "level_horizon":
                self._submit(name, self._level_horizon)
            elif name == "focus_now":
                if self.onvif is None:
                    raise RuntimeError("focus control is not configured")
                # On the capture thread: a run holds the phone and gathers frames,
                # and must never do so alongside a still.
                self._focus_requested = True
                self.capture_requested.set()
            elif name == "record_flat_field":
                if self.flat is None:
                    raise RuntimeError("the flat field is switched off (FLAT_FIELD_FILE is empty)")
                # On the capture thread, like a still: it takes the same hold
                # and must never gather frames alongside one.
                self._flat_requested = True
                self.capture_requested.set()
            elif name == "reboot_phone":
                if self.rebooter is None:
                    raise RuntimeError("the rebooter is not configured")
                # manual=True: overrides the switch, the budget and the latch.
                self._submit(name, lambda: self.rebooter.attempt(True))
            elif name in ("autofocus", "focus_position"):
                self._set_focus(name, value)
            else:
                self._apply_setting(name, value)
        except Exception:
            log.exception("control %r failed", name)

    def _apply_setting(self, name, value):
        try:
            known = self.s.apply_control(name, value)
        except ValueError as e:
            # A value outside a select's options, which only a script or a
            # hand-published message can produce. Say so, and put the real
            # value back so Home Assistant does not keep showing the rejected
            # one as if it had taken effect.
            log.warning("rejected control %s: %s", name, e)
            self.publish_state({name: self._stored(name)})
            return
        if not known:
            log.warning("ignoring unknown control %r", name)
            return
        # Echo what was stored, not what arrived: the black-level channels live
        # inside a tuple and are coerced on the way in.
        stored = self._stored(name)
        log.info("control %s = %r", name, stored)
        self._persist()
        self.publish_state({name: stored})

    def _stored(self, name):
        """The value a control holds now, as it is published. A black-level
        channel is not an attribute of its own but one slot of `black_level`,
        which always has a value: whether it is in effect is
        `black_level_override_on`, not the presence of the number."""
        channel = BLACK_LEVEL_CHANNELS.get(name)
        if channel is None:
            return getattr(self.s, name, None)
        return self.s.black_level[channel]

    def _submit(self, name, fn, *, key=None, latest_wins=False):
        """Queue `fn` for the device worker, unless the same action is already
        queued or running. Returns whether anything was queued.

        Nothing authenticates a control, and the worker is a single thread with
        an unbounded queue that the backgrounding hooks share. Without this, a
        misfiring automation or a button that fires twice parks one device
        round trip per press ahead of those hooks, and backgrounding silently
        stops while the app keeps streaming. With it, each action has at most
        one entry in play, whichever interface asked.

        `key` says which requests are the same action, `name` by default. A
        repeat is refused and logged: the entry already in play does what it
        asked for. `latest_wins` is for an action carrying a value, where a
        later request is not a repeat but a change of mind. That request is
        kept, replacing any request already kept for the same key, and runs
        once the action in play finishes, so the last value asked for is the
        one applied and at most two runs are ever in play for the key.
        """
        key = key or name
        with self._device_lock:
            if key in self._device_busy:
                if latest_wins:
                    self._device_again[key] = (name, fn)
                    log.info("device action %r is already queued or running; "
                             "the latest request will run after it", name)
                else:
                    log.info("device action %r is already queued or running; "
                             "ignoring the repeat", name)
                return False
            self._device_busy.add(key)
        try:
            self._device_executor.submit(self._run_device_action, name, fn, key)
        except RuntimeError:
            # The worker has been shut down: nothing more will ever run.
            with self._device_lock:
                self._device_busy.discard(key)
            log.info("device action %r not queued: shutting down", name)
            return False
        return True

    def _maybe_open_app(self):
        """Ask the opener, unless it is already busy.

        A phone that has just rebooted cannot answer over the cable for a
        minute or two while the developer image remounts, and that attempt sits
        blocked for as long as the mount's own timeout. Without this guard the
        health tick would queue a fresh one every 30 s behind it.
        """
        # _submit would refuse a second attempt anyway; checked here first only
        # so that a mount blocking for minutes does not log a refusal on every
        # tick. An unlocked read: a stale answer only means one such log line.
        if self.opener is None or "open_app" in self._device_busy:
            return
        self._submit("open_app", self._open_app)

    def _open_app(self):
        self.opener.attempt(False)

    def _maybe_reboot_phone(self):
        """Feed the rebooter, and queue a reboot once the wedge is certain.

        The observation happens every tick and costs nothing -- it is a counter
        the phone manager already keeps. The attempt is queued on the device
        worker like any other phone call, because it blocks for the settle time
        while the phone boots.
        """
        if self.rebooter is None or self.phone is None:
            return
        self.rebooter.observe(getattr(self.phone, "dvt_failures", 0))
        # Checked here as well as inside attempt() so that a healthy phone never
        # occupies the single device worker, which a mount can block for minutes.
        if self.rebooter.wedged_for < WEDGE_FOR_S or "reboot_phone" in self._device_busy:
            return
        self._submit("reboot_phone", lambda: self.rebooter.attempt(False))

    def _run_device_action(self, name, fn, key):
        try:
            fn()
        except Exception as e:
            log.exception("device action %r failed", name)
            self._event({"ts": time.time(), "control": name,
                         "error": f"{type(e).__name__}: {e}"})
        finally:
            self._device_action_done(key)

    def _device_action_done(self, key):
        # Under the same lock _submit checks, so a latest-wins request arriving
        # as this action finishes is either kept here as its follow-up or finds
        # the key already free: it cannot be kept after this check and then
        # never run. A plain repeat in that instant is still refused, exactly
        # as it would have been a moment earlier.
        with self._device_lock:
            follow_up = self._device_again.pop(key, None)
            if follow_up is None:
                self._device_busy.discard(key)
                return
        # The key stays busy: the follow-up takes over its place.
        try:
            self._device_executor.submit(self._run_device_action, *follow_up, key)
        except RuntimeError:
            with self._device_lock:
                self._device_busy.discard(key)
            log.info("device action %r not queued: shutting down", follow_up[0])

    def _screenshot(self):
        if self.phone is None:
            raise RuntimeError("phone management is not configured")
        # Converted once, then used for both: the retained MQTT message and the
        # HTTP route must be the same image, and the phone's lossless PNG is
        # ~9.5 MB, which is no size for either.
        jpeg = render.png_to_jpeg(self.phone.screenshot(), SCREENSHOT_JPEG_QUALITY)
        self._last_screenshot_jpeg = jpeg
        self.mqtt.publish_screenshot(jpeg)

    def _relaunch(self):
        if self.phone is None:
            raise RuntimeError("phone management is not configured")
        # Report whether it was already running: this button is for a dead app,
        # and relaunching a live one loses the stream it was already serving.
        # Best effort only, and never allowed to fail the launch: is_running
        # goes over the DVT channel, which needs the developer image mounted,
        # and that is least likely to work on exactly the reboot that killed
        # the app -- the moment this button matters most.
        try:
            was_running = self.phone.is_running()
        except Exception as e:
            log.info("could not check whether the app was running: %s", e)
            was_running = None
        self.phone.launch()
        # Deliberately an event, not a state: relaunching leaves streaming off
        # until someone taps Start streaming in the app.
        self._event({"ts": time.time(), "control": "relaunch_app",
                     "result": "launched", "was_running": was_running})

    def _prepare_phone(self):
        if self.phone is None:
            raise RuntimeError("phone management is not configured")
        steps = self.phone.prepare(launch=True)
        self._event({"ts": time.time(), "control": "prepare_phone", "steps": steps})

    def _persist(self):
        if self.store is not None:
            self.store.save(self.s)

    def _set_focus(self, name, value):
        """A write to either focus control: store it, then bring the phone in line.

        The value written is what the camera should do, and it stays in force
        whether or not the phone can hear it now. At night the app spends most of
        its time backgrounded, where it answers nothing, and a write that was
        undone because the phone was asleep was indistinguishable from one that
        never took. So the setting is kept and persisted, and applied now if the
        phone answers, or at the start of the next session if it does not --
        where it is also re-checked, because a relaunched app comes back on
        autofocus whatever it was told before.
        """
        try:
            if name == "autofocus":
                self.s.apply_control("autofocus", _coerce_bool(value))
            else:
                position = float(value)
                if not 0.0 <= position <= 1.0:
                    raise ValueError(f"focus_position must be between 0 and 1, not {value!r}")
                self.s.apply_control("focus_position", position)
                # Writing a position means "focus there", which autofocus would
                # immediately undo -- the same way writing a black-level channel
                # switches the override on.
                self.s.apply_control("autofocus", False)
        except (TypeError, ValueError) as e:
            # Rejected before anything reaches the device worker: a bad value
            # must not cost an ONVIF round trip or time on the single worker.
            log.warning("rejected control %s: %s", name, e)
            self.publish_state({name: getattr(self.s, name, None)})
            return
        log.info("control autofocus = %r, focus_position = %r",
                 self.s.autofocus, self.s.focus_position)
        self._persist()
        self.publish_state({"autofocus": self.s.autofocus,
                            "focus_position": self.s.focus_position})
        # latest_wins: a value, not a press. Writes arriving while one is being
        # applied collapse into one follow-up, which reads the settings as they
        # are by then -- so the last value asked for is the one the phone gets.
        self._submit("focus", self._apply_focus, latest_wins=True)

    def _apply_focus(self):
        """Make the phone's focus match the settings. Runs on the device worker.

        Retries briefly, because this runs straight after the app is woken for a
        session and the app takes up to a second to answer. A phone that still
        does not answer is left for the next session, which tries again; that is
        reported as an event, never raised.
        """
        if self.onvif is None:
            return
        # A focus run owns the lens while it sweeps; the session it opens must not
        # put the held position back in the middle of it. Taken, not just checked,
        # so a run started meanwhile waits for this to finish instead.
        if not self._lens_lock.acquire(blocking=False):
            return
        try:
            self._apply_focus_attempts()
        finally:
            self._lens_lock.release()

    def _apply_focus_attempts(self):
        for attempt in range(FOCUS_ATTEMPTS):
            try:
                self._apply_focus_once()
                return
            except _FocusRejected as e:
                log.warning("focus: %s", e)
                self._event({"ts": time.time(), "control": "focus", "error": str(e)})
                return
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                if attempt + 1 < FOCUS_ATTEMPTS:
                    self._sleep(FOCUS_RETRY_S)
        log.info("focus: the phone did not answer (%s); will apply at the next session", error)
        self._event({"ts": time.time(), "control": "focus", "error": error})

    def _apply_focus_once(self):
        mode = self.onvif.get_autofocus()
        if self.s.autofocus:
            if mode != "AUTO" and not self.onvif.set_autofocus("AUTO"):
                raise _FocusRejected("the camera rejected autofocus")
            return
        if mode != "MANUAL" and not self.onvif.set_autofocus("MANUAL"):
            raise _FocusRejected("the camera rejected manual focus")
        target = self.s.focus_position
        # Never trusted, the phone's reported position: measured 2026-09-28, after
        # the app's capture session restarted it reported MANUAL at 0.804 while the
        # lens sat elsewhere, and the stills went soft. So every session drives the
        # lens -- away and back, because a move to where the phone believes it
        # already is may do nothing at all.
        nudge = target + FOCUS_NUDGE if target + FOCUS_NUDGE <= 1.0 else target - FOCUS_NUDGE
        for position in (round(nudge, 3), target):
            if not self.onvif.move_focus(position):
                raise _FocusRejected(f"the camera rejected moving the lens to {position}")

    def _follow_autofocus(self):
        """While autofocus is on, keep focus_position at where it put the lens.

        So that switching autofocus off holds the lens about where it was, which
        is what that switch always meant, and so the number shows a person what
        autofocus chose -- in daylight, where it focuses well, that is the value
        worth keeping for the night. Read as a session ends rather than as it
        starts: autofocus has had the whole capture to settle by then.
        """
        if self.onvif is None or not self.s.autofocus:
            return
        if not self._lens_lock.acquire(blocking=False):
            return
        try:
            position = self.onvif.focus_position()
        except Exception as e:
            log.info("focus: could not read the lens position: %s: %s", type(e).__name__, e)
            return
        finally:
            self._lens_lock.release()
        if position is None or abs(position - self.s.focus_position) < FOCUS_TOLERANCE:
            return
        self.s.apply_control("focus_position", round(position, 3))
        self._persist()
        self.publish_state({"focus_position": self.s.focus_position})

    # -- health -------------------------------------------------------------

    def on_stream_state(self, state):
        """Ingest calls this on every change; published at once, not at the
        next interval, so an outage is visible within the health tick.

        The whole health reading goes with it, not just this one value:
        camera_online, the booleans and last_frame are all derived from the same
        state, and publishing only this left them contradicting it until the next
        health tick -- up to 30 s, and after every restart, because the first
        tick runs before the first probe has put a frame in the buffer.
        """
        self.stream_state = state
        d = self.health_state()
        # The value we were told, not a re-read: Upstream decides what changed and
        # in what order, and a newer state will arrive through here on its own.
        d["stream_state"] = state
        self.publish_state(d)
        # Only streaming_off can be fixed by keys: the app is open and idle. A
        # refusal means backgrounded, locked or not in front, which is another
        # flow's job, and the active states are already streaming. The starter
        # re-checks all of it anyway; this is only the trigger.
        if state == "streaming_off" and self.starter is not None:
            # Its own key: this attempt honours the pause switch and the rate
            # limit, so it must not swallow a person's press, which overrides
            # both. Reported under the button's name all the same.
            self._submit("start_streaming", lambda: self.starter.attempt(False),
                         key="start_streaming:automatic")
        # Nothing answered on the app's page at all, which is what a phone that
        # rebooted looks like. Asked here as well as on the battery tick so the
        # app is opened in seconds rather than after the next five-minute
        # sweep. The opener re-checks over the cable, where a suspended app
        # still counts as running, so a deliberate backgrounding is left alone.
        if state in ("backgrounded", "unavailable"):
            self._maybe_open_app()

    def health_state(self, now=None):
        now = time.time() if now is None else now
        newest = self.buffer.newest_ts()
        state = self.upstream.state(now) if self.upstream else self.stream_state
        d = {
            # A timestamp rather than an age: Home Assistant turns this into a
            # relative time that keeps counting between health ticks, where a
            # published age is stale the moment it lands.
            "last_frame": (None if newest is None else
                           datetime.fromtimestamp(newest, tz=UTC).isoformat()),
            "camera_online": self._camera_online(state, now, newest),
            # Published so the orphaned-socket case can be alerted on well before
            # the self-restart is reached, and so a plain unplugged cable is
            # visibly different from a broken daemon.
            "cable_state": self._cable_state,
        }
        if state is not None:
            d["stream_state"] = state
        if self.upstream is not None:
            held = self.upstream.holders(now)
            d["holders"] = ", ".join(held) or "none"
            d["duty_1h"] = self.upstream.duty_1h(now)
            d["tick_age"] = round(self.upstream.tick_age(now), 1)
            d.update(self._boolean_state(state, held))
        # Published even without a keyboard, because every declared entity must
        # carry a value: one that is never published shows as unknown forever,
        # and "unknown" reads as a fault rather than as "not fitted".
        d["stream_start"] = self.starter.state if self.starter is not None else "idle"
        d["stream_start_attempts"] = (self.starter.attempts_last_hour
                                      if self.starter is not None else 0)
        d["keyboard_connected"] = bool(self.keyboard is not None and self.keyboard.connected())
        d["app_open"] = self.opener.state if self.opener is not None else "idle"
        d["phone_reboot"] = self.rebooter.state if self.rebooter is not None else "idle"
        d["flat_field"] = self._flat_state
        d["focus_run"] = self._focus_state
        d["colour_reference"] = self._colour_state
        d["horizon"] = self._horizon_state
        d["emergency_background"] = (self.emergency.state
                                     if self.emergency is not None else "idle")
        d["backgrounded_by_keyboard"] = self.backgrounding.backgrounded_by_keyboard
        d["phone_reboots"] = (self.rebooter.reboots_last_day
                              if self.rebooter is not None else 0)
        # A rising count means the phone's developer services are not answering,
        # which is what silently stopped backgrounding for 24 hours on 2026-09-14.
        d["dvt_failures"] = getattr(self.phone, "dvt_failures", 0) if self.phone else 0
        if getattr(self, "live_fps", None) is not None:
            d["live_fps"] = self.live_fps()
        # With an upstream, only while streaming: probe frames a minute apart
        # would publish 0.02. Without one, keep publishing it unconditionally
        # -- that's the old behaviour, and every existing caller still relies
        # on it since on-demand's idle state doesn't exist for them.
        if self.upstream is None:
            d["frame_rate"] = round(estimate_fps(self.buffer), 1)
        elif state in ACTIVE_STATES:
            d["frame_rate"] = round(estimate_fps(
                self.buffer, since=self.upstream.session_start_ts()), 1)
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
            self._note_cable_failure(e)
            # The cable is the only thing that can answer this, so with it gone
            # the honest answer is "unknown", not "no".
            self._app_running = None
            return {}
        self._note_cable_ok()
        # Asked here rather than on the health tick because this is a usbmux
        # round trip. It is the one signal that separates an app iOS has
        # suspended from one it has killed: over WiFi both simply refuse.
        try:
            self._app_running = bool(self.phone.is_running())
        except PhoneUnavailable as e:
            log.info("app state unavailable: %s", e)
            self._app_running = None
        # The step before the keys. An app that is not running cannot be started
        # by them, and nothing else in this service opens it, so a phone that
        # rebooted would otherwise wait for a person. False only: None means the
        # cable could not answer, and a cold start on a guess kills a stream
        # that was merely suspended.
        if self._app_running is False:
            self._maybe_open_app()
        # Already on the health thread, and the policy only decides -- any phone
        # call it makes here is the release path, which must happen promptly or
        # the app stays asleep.
        self.backgrounding.on_battery(b.get("level"))
        return {"battery": b.get("level"), "battery_temperature": b.get("temperature"),
                "charging": bool(b.get("charging")),
                "battery_current_ma": b.get("current_ma"),
                "charger_watts": b.get("charger_watts"),
                "battery_health_pct": b.get("health_pct")}

    def _camera_online(self, state, now, newest):
        """Whether the camera is working, which is not the same as sending frames.

        Upstream reports `backgrounded` as offline, and for a phone that wandered
        into the background on its own that is right: nothing will bring it back.

        But when this service backgrounded the app *deliberately* to save
        battery, the camera is fine -- it wakes for the next capture on its own,
        proven on the device. Calling that offline would fire the five-minute
        offline alert every night the battery ran low, and an alert that cries
        wolf nightly is one nobody reads.

        Only a backgrounding this service performed counts: `backgrounded` is
        set by our own successful call, so a phone somebody else backgrounded
        still reads offline.
        """
        if state == "backgrounded" and self.backgrounding.backgrounded:
            return True
        if self.upstream is not None:
            return self.upstream.camera_online(now)
        return (self.stream_state == "streaming"
                and newest is not None and now - newest < self.fresh_s)

    # -- session hooks, for Upstream ------------------------------------------
    #
    # Upstream calls these on its tick thread, which must never block: a usbmux
    # round trip there would stall every hold, probe and state publication behind
    # it. They hand the work to the device worker, the same single thread that
    # already serialises MQTT-driven device actions, so two phone calls can never
    # overlap on the cable.

    # Both hooks decide on the worker, when the action runs, never here when it
    # is queued. Measured 2026-09-28: a viewer left and another arrived 0.2 s
    # later. The close queued a background; the open, asking here whether the
    # app was backgrounded, was told "not yet" and queued nothing; then the
    # background ran under the new session, which nothing would ever wake.

    def on_session_open(self):
        # Always queued, even when the app is awake now: a background already
        # queued ahead of it may put it to sleep before this runs. When it
        # finds nothing to wake it costs no phone call.
        self._device_executor.submit(self.backgrounding.on_session_open)
        # After the wake-up, on the same worker, so the app is listening. Every
        # session, not only after a write: a relaunched app comes back on
        # autofocus, and this is what puts it back.
        self._submit("focus", self._apply_focus, latest_wins=True)

    def on_session_close(self):
        # Before any background, which is queued behind it: the app must still
        # be awake to answer.
        self._device_executor.submit(self._follow_autofocus)
        if self.backgrounding.active:
            self._device_executor.submit(self._background_if_still_closed)

    def _background_if_still_closed(self):
        # The worker may reach this long after the session closed. A session
        # open again by then has frames to fetch, and sleeping the app under it
        # would leave it dark.
        if self.upstream is not None and self.upstream.session_start_ts() is not None:
            log.info("not backgrounding: a session opened again before the worker got to it")
            return
        self.backgrounding.on_session_close()

    def _boolean_state(self, state, held):
        """The independent axes behind `stream_state`, as plain booleans.

        `stream_state` says what the camera is doing in one value; these say which
        part is broken, which is the question at 1am. They are not a decomposition
        of it -- the cable and the WiFi fail separately, and the app can be alive
        on a phone that is sending nothing.
        """
        # A refusal proves the phone's IP stack answered: something is listening
        # on the network even though the app's port is not. A timeout does not,
        # unless a frame came first, which is what `stalled` means.
        reachable_wifi = state in ACTIVE_STATES + ("ready", "streaming_off", "stalled",
                                                   "backgrounded")
        return {
            "reachable_over_wifi": reachable_wifi,
            "reachable_over_cable": self._cable_state == "ok",
            # Only the cable can answer this. Over WiFi a suspended app and a
            # killed one both simply refuse. None means "not asked yet", which is
            # published as unknown rather than guessed at.
            "app_running": self._app_running,
            # Reachable and willing to send. False for streaming_off (the Start
            # streaming button has not been tapped) and for backgrounded.
            "app_streaming": state in ACTIVE_STATES + ("ready",),
            "capturing_stills": any(h.startswith("capture") for h in held),
            "being_viewed": any(h.startswith("viewer") for h in held),
            # Whether the power-saving policy is engaged, which is not the same
            # as the switch being on: with a full battery the switch is on and
            # this is off.
            "backgrounding_active": self.backgrounding.active,
        }

    # -- cable self-heal ------------------------------------------------------

    def _note_cable_ok(self):
        if self._cable_down_since is not None:
            log.info("cable recovered after %.0fs", self._clock() - self._cable_down_since)
        self._cable_down_since = None
        self._cable_state = "ok"

    def _note_cable_failure(self, exc):
        """Record a cable failure and restart the process if every guard agrees.

        Only a usbmuxd failure starts the clock. An unplugged phone is reported
        but never counted: it is the normal state whenever the cable is out, and
        restarting for it would turn unplugging the phone into a restart loop.
        """
        if not is_usbmuxd_down(exc):
            self._cable_down_since = None
            self._cable_state = "no_device"
            return

        now = self._clock()
        self._cable_state = "usbmuxd_down"
        if self._cable_down_since is None:
            self._cable_down_since = now
            log.warning("usbmuxd unreachable; the socket mount may be orphaned: %s", exc)
        if self._cable_restart_needed(now):
            self._last_cable_restart = now
            self._request_restart()

    def _cable_restart_needed(self, now):
        if now - self._started_at < CABLE_MIN_UPTIME_S:
            return False
        if now - self._cable_down_since < CABLE_FAILING_FOR_S:
            return False
        if (self._last_cable_restart is not None
                and now - self._last_cable_restart < CABLE_RESTART_COOLDOWN_S):
            return False
        return True

    # -- the colour reference ------------------------------------------------

    def _colour_gains(self, stacked, black, gains, weight):
        """The white-balance gains for this still: the reference's at night when it
        is chosen and the view still matches, otherwise the ones already computed."""
        if self.colour is None:
            return gains
        if self.s.white_balance != "reference":
            self._set_colour_state("ready" if self.colour.ready else "none")
            return gains
        if not weight > 0:
            # Daylight: the phone's white balance has light to work with, and whether
            # the view matches a night reference is not worth asking.
            self._set_colour_state("ready" if self.colour.ready else "none")
            return np.ones(3)
        try:
            found, state = self.colour.gains(np.clip(stacked - black, 0, None))
        except Exception:
            # Never worth the still: it goes out as the phone shot it.
            log.exception("colour reference: could not be applied; this still is as shot")
            return np.ones(3)
        if found is None:
            self._set_colour_state(state)
            return np.ones(3)
        self._set_colour_state("applied")
        # Faded with the night render, so dusk eases into it.
        return 1 + weight * (np.asarray(found) - 1)

    def _set_colour_reference(self):
        if self._last_render is None:
            return self._set_colour_state("rejected_no_still")
        stacked, black, weight, gains = self._last_render
        if not weight > 0:
            return self._set_colour_state("rejected_daylight")
        region = (self.s.colour_region_x0, self.s.colour_region_y0,
                  self.s.colour_region_x1, self.s.colour_region_y1)
        # With its gains: the colour approved is the one shown, which may already be
        # corrected -- by this reference, or by another white balance mode.
        reason = self.colour.record(np.clip(stacked - black, 0, None) * gains, region)
        if reason is not None:
            log.info("colour reference: refused, %s", reason.replace("_", " "))
            return self._set_colour_state(f"rejected_{reason}")
        log.info("colour reference: recorded region %s", region)
        return self._set_colour_state("ready")

    def _set_colour_state(self, state):
        if state != self._colour_state:
            self._colour_state = state
        self.publish_state({"colour_reference": state})
        return state

    # -- levelling the view --------------------------------------------------

    def _level_horizon(self):
        """Measure the last still's lean and add it to the rotation that still was
        turned by. See horizon.py.

        Added to that rotation, not to the one in use now: a second press before the
        next still, or a rotation typed since, would otherwise count the same lean
        twice. A still made with this rotation is asked for at once, so the result
        shows without waiting for the schedule."""
        if self._last_shown is None:
            return self._set_horizon_state("rejected_no_still")
        jpeg, weight, shown_with = self._last_shown
        if weight is None or weight > LEVEL_MAX_WEIGHT:
            return self._set_horizon_state("rejected_night")
        bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        try:
            lean = horizon.estimate_tilt(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        except horizon.NoClearHorizon as e:
            log.info("level horizon: refused, %s", e)
            return self._set_horizon_state("rejected_no_clear_lines")
        rotation = round(min(MAX_ROTATION, max(-MAX_ROTATION, shown_with + lean)), 1)
        log.info("level horizon: the still leans %+.2f degrees; rotation %.1f -> %.1f",
                 lean, shown_with, rotation)
        # Written as a person's write is: persisted, and echoed to every client.
        self._apply_setting("rotation", rotation)
        self.capture_requested.set()
        return self._set_horizon_state("levelled")

    def _set_horizon_state(self, state):
        self._horizon_state = state
        self.publish_state({"horizon": state})
        self._event({"ts": time.time(), "control": "level_horizon", "result": state})
        return state

    # -- the service's own focus ---------------------------------------------

    def run_focus(self, reason="button"):
        """Find the sharpest far-field lens position and hold it. See focus.py.

        Runs on the capture thread under a capture hold, and owns the lens for the
        whole run: a focus already being applied finishes first, and none starts
        until it is over. Any failure puts the lens back as it was -- autofocus on
        again if it was on -- and changes no setting; a result is kept only if it
        beats the position it would replace, and is then written through
        focus_position exactly as a person's write is.
        """
        if self.onvif is None:
            return self._set_focus_run("failed_phone")
        with self._lens_lock:
            return self._run_focus_owning_the_lens(reason)

    def _run_focus_owning_the_lens(self, reason):
        was_auto, previous = self.s.autofocus, self.s.focus_position
        self._set_focus_run("running")
        hold = (self.upstream.hold("capture") if self.upstream is not None
                else contextlib.nullcontext())
        # Everything inside the hold, restoring included: once it is released the
        # battery policy may background the app, which then refuses every call.
        with hold:
            try:
                self._wake_for_focus()
                position, curve = focus.search(self._focus_measure)
                better = self._beats_the_held_position(position, previous, was_auto)
            except focus.NoClearPeak as e:
                log.info("focus run (%s): no clear peak, nothing changed: %s", reason,
                         _curve_text(e.curve))
                return self._focus_failed("unchanged_no_clear_peak", was_auto, previous)
            except _NoFocusFrames as e:
                log.warning("focus run (%s): %s; nothing changed", reason, e)
                return self._focus_failed("failed_no_frames", was_auto, previous)
            except Exception as e:
                log.warning("focus run (%s): the phone failed it (%s: %s); nothing changed",
                            reason, type(e).__name__, e)
                return self._focus_failed("failed_phone", was_auto, previous)
            if not better:
                log.info("focus run (%s): %.3f did not beat the held %.3f; nothing changed. "
                         "Curve %s", reason, position, previous, _curve_text(curve))
                return self._focus_failed("unchanged_already_sharpest", was_auto, previous)
            log.info("focus run (%s): lens %.3f (was %.3f), curve %s", reason, position,
                     previous, _curve_text(curve))
            self.s.apply_control("focus_position", round(position, 3))
            self.s.apply_control("autofocus", False)
            self._persist()
            self.publish_state({"autofocus": False, "focus_position": self.s.focus_position})
            try:
                self.onvif.move_focus(self.s.focus_position)
            except Exception as e:
                # Held regardless: the next session puts it there.
                log.info("focus run: could not move the lens to its result now: %s", e)
            return self._set_focus_run("focused")

    def _wake_for_focus(self):
        """Wait for the app to answer, then take manual focus.

        At dusk the battery policy has usually backgrounded the app, and a hold
        only asks for a session: every call is refused until that session has
        woken it, and for up to a second after (see FOCUS_RETRY_S).
        """
        if self.upstream is not None:
            if self.upstream.wait_for_frames(1, time.time(), CAPTURE_OPEN_TIMEOUT_S) < 1:
                raise _NoFocusFrames("the phone sent nothing after the run asked for it")
        for attempt in range(FOCUS_ATTEMPTS):
            try:
                if not self.onvif.set_autofocus("MANUAL"):
                    raise _FocusRejected("the camera refused manual focus")
                return
            except _FocusRejected:
                raise
            except Exception:
                if attempt + 1 == FOCUS_ATTEMPTS:
                    raise
                self._sleep(FOCUS_RETRY_S)

    def _beats_the_held_position(self, position, previous, was_auto):
        """Whether `position` is sharper than the one it would replace, now.

        Contrast changing through a run -- lights coming on at dusk, the phone
        changing exposure -- can fake a peak. So the held position is measured
        either side of the result, which cancels a steady drift, and the result
        is kept only if it scores at least as well. With autofocus on there is no
        held position to defend.
        """
        if was_auto or abs(position - previous) < 0.005 or not focus.NEAR <= previous <= focus.FAR:
            return True
        before = self._focus_measure(previous)
        new = self._focus_measure(position)
        after = self._focus_measure(previous)
        log.info("focus run: result %.3f scores %.3g; the held %.3f scores %.3g and %.3g",
                 position, new, previous, before, after)
        return new >= (before + after) / 2

    def _focus_measure(self, position):
        if not self.onvif.move_focus(position):
            raise _FocusRejected(f"the camera refused to move the lens to {position}")
        self._sleep(FOCUS_SETTLE_S)
        since = time.time()
        if self.upstream is not None:
            self.upstream.wait_for_frames(FOCUS_FRAMES, since, FOCUS_PROBE_TIMEOUT_S)
        frames = [f for f in self.buffer.recent(FOCUS_FRAMES) if f.ts >= since]
        if len(frames) < FOCUS_MIN_FRAMES:
            raise _NoFocusFrames(f"only {len(frames)} frames arrived at lens {position}")
        return focus.score(focus.average(frames))

    def _focus_failed(self, state, was_auto, previous):
        try:
            if was_auto:
                self.onvif.set_autofocus("AUTO")
            else:
                self.onvif.move_focus(previous)
        except Exception as e:
            log.info("focus run: could not put the lens back (%s: %s); the next session "
                     "will", type(e).__name__, e)
        return self._set_focus_run(state)

    def _set_focus_run(self, state):
        self._focus_state = state
        self.publish_state({"focus_run": state})
        return state

    def _dusk_focus_due(self, now=None):
        """Whether the evening's focus run is due, from the last still's light."""
        if not self.s.focus_at_dusk or self.s.autofocus or self.onvif is None:
            return False
        weight, before = self._last_weight, self._prev_weight
        if weight is None or not 0 < weight < DUSK_MAX_WEIGHT:
            return False
        # Dusk, not dawn: in the morning the light rises back through the same
        # band, and a run then would use up the evening's.
        local = time.localtime(now)
        if before is None or not weight > before or local.tm_hour < 12:
            return False
        today = time.strftime("%Y-%m-%d", local)
        if today != self._dusk_day:
            self._dusk_day, self._dusk_tries, self._dusk_done = today, 0, False
        return not self._dusk_done and self._dusk_tries < DUSK_TRIES

    def _dusk_ran(self, result):
        self._dusk_tries += 1
        self._dusk_done = self._dusk_done or result == "focused"

    # -- the flat field -----------------------------------------------------

    def record_flat_field(self, now=None):
        """Gather a minute of frames and keep their fixed grain. See flat.py.

        Only frames from after the press are used: the lens has to be covered
        first, and a viewer's session may already hold frames of the view.
        """
        self._set_flat_state("recording")
        now = time.time() if now is None else now
        hold = (self.upstream.hold("capture") if self.upstream is not None
                else contextlib.nullcontext())
        with hold:
            if self.upstream is not None:
                floor = max(self.upstream.session_start_ts() or now, now)
                self.upstream.wait_for_frames(FLAT_FRAMES, floor, FLAT_WINDOW_S)
                frames = [f for f in self.buffer.recent(FLAT_FRAMES) if f.ts >= floor]
            else:
                frames = [f for f in self.buffer.recent(FLAT_FRAMES) if f.ts >= now]
        # Whatever the outcome, the lens was covered, and that throws the phone's
        # white balance: measured 2026-09-28, stills after a flat field were cyan
        # (green/red 1.48 against 0.97), and at night there is too little light
        # for iOS to find its way back. Restarting the app's capture session
        # makes it look again; that cured it.
        self._submit("reset_white_balance", self._reset_white_balance)
        if len(frames) < FLAT_MIN_FRAMES:
            log.warning("flat field: only %d frames arrived; not recorded", len(frames))
            return self._set_flat_state("rejected_no_frames")
        reason = self.flat.record(stack.stack_frames(frames))
        if reason is not None:
            log.warning("flat field: refused, %s; the previous one is kept",
                        reason.replace("_", " "))
            return self._set_flat_state(f"rejected_{reason}")
        log.info("flat field: recorded from %d frames", len(frames))
        return self._set_flat_state("ready")

    def _reset_white_balance(self):
        """Background the app and bring it back, so the phone re-reads the light.

        Runs on the device worker, after the recording's hold is released. The
        battery policy may have wanted the app asleep, so it is asked again once
        the app is back in front.
        """
        if self.phone is None:
            return
        self.phone.background()
        self._sleep(WB_RESET_PAUSE_S)
        self.phone.foreground()
        log.info("flat field: restarted the camera so its white balance re-reads the scene")
        self.backgrounding.backgrounded = False
        self._background_if_still_closed()

    def _set_flat_state(self, state):
        self._flat_state = state
        self.publish_state({"flat_field": state})
        self._event({"ts": time.time(), "control": "record_flat_field", "result": state})
        return state

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
                if self._flat_requested:
                    self._flat_requested = False
                    self.record_flat_field()
                elif self._focus_requested:
                    self._focus_requested = False
                    self.run_focus("button")
                else:
                    self.produce_still(None)
                    if self._dusk_focus_due():
                        # Counted whatever happens, or a run that raised would be
                        # tried again after every still of the evening.
                        result = "failed_phone"
                        try:
                            result = self.run_focus("dusk")
                        finally:
                            self._dusk_ran(result)
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
                # Measured after a phone restart on 2026-09-16: the cable cannot
                # answer for a minute or so after the phone boots, so the
                # attempt triggered by the state change can only say "cannot
                # tell". Retried here rather than left until the next
                # five-minute battery sweep; the opener's own rate limit bounds
                # what this can actually do to the phone.
                if self.stream_state in ("backgrounded", "unavailable"):
                    self._maybe_open_app()
                self._maybe_reboot_phone()
                if time.monotonic() - last_battery >= battery_every:
                    last_battery = time.monotonic()
                    self.publish_state(self.phone_state())
            except Exception:
                log.exception("health tick failed")
            stop.wait(every)

    def close(self):
        # Queued device actions are dropped, not left to drain. The worker is a
        # non-daemon thread the interpreter joins after main() returns, so each
        # action still queued -- an ONVIF or cable round trip, or a developer
        # image mount that can block for minutes -- would otherwise run after
        # the shutdown budget is spent, and the container's grace period would
        # end in a kill. The one action already running cannot be interrupted
        # and still finishes.
        self._device_executor.shutdown(wait=False, cancel_futures=True)
