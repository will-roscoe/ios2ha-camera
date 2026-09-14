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

from . import measure, render, stack
from .backgrounding import BackgroundingPolicy
from .phone import PhoneUnavailable
from .settings import BLACK_LEVEL_CHANNELS, CONTROLS
from .states import ACTIVE_STATES
from .upstream import CAPTURE_OPEN_TIMEOUT_S

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
                 device_executor=None, request_restart=None, clock=None):
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
        self.s = settings
        # Reads the switch and both thresholds live, because all three are Home
        # Assistant controls that can change between one capture and the next.
        self.backgrounding = BackgroundingPolicy(
            phone,
            lambda: bool(getattr(self.s, "backgrounding", False)),
            lambda: float(getattr(self.s, "background_below_pct", 50.0)),
            lambda: float(getattr(self.s, "resume_above_pct", 80.0)),
        )
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
        self._state_lock = threading.Lock()
        self._last_jpeg = None
        self._device_executor = device_executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="device")

    @property
    def last_still_jpeg(self):
        """The most recent stacked still, or None before the first one.

        Read by the live server's /still.jpg so a camera thumbnail shows the
        stacked image rather than a single raw frame, without touching the phone.
        """
        return self._last_jpeg

    # -- publishing ---------------------------------------------------------

    def publish_state(self, d):
        """Publish and remember, so a reconnect can replay the current values."""
        if not d:
            return
        with self._state_lock:
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
        self.publish_state(d)
        # A snapshot, never the live dict: this runs on the MQTT network thread
        # while the capture and health threads are still writing, and iterating
        # the dict itself would raise "changed size during iteration" straight
        # into a caller that only logs it -- silently losing the republish.
        with self._state_lock:
            snapshot = dict(self._last_state)
        if snapshot:
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
            return self._produce_still(now_fps, now)
        except Exception as e:
            log.exception("capture failed")
            return self._event({"ts": time.time() if now is None else now,
                                "error": f"{type(e).__name__}: {e}"})

    def _produce_still(self, now_fps=None, now=None):
        now = time.time() if now is None else now
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
            mean = np.mean(probe, axis=0)

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
        rgb = render.render(stacked, self.s, applied=(black, gains), night_weight=weight)
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
            # Per cent, to match the sensor's declared unit.
            "highlight_clip": round(float((stacked.max(axis=2) >= 250).mean()) * 100, 2),
            "noise": noise,
            "snr": snr,
            "motion": (round(measure.motion_level(probe[-2], probe[-1]), 3)
                       if len(probe) >= 2 else 0.0),
            "focus_sharpness": round(measure.focus_sharpness(stacked), 1),
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
            elif name == "autofocus":
                self._submit(name, lambda: self._autofocus(bool(value)))
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
            self.publish_state({name: getattr(self.s, name, None)})
            return
        if not known:
            log.warning("ignoring unknown control %r", name)
            return
        # Echo what was stored, not what arrived: the black-level channels live
        # inside a tuple and are coerced on the way in.
        channel = BLACK_LEVEL_CHANNELS.get(name)
        stored = (self.s.black_level_override[channel] if channel is not None
                  else getattr(self.s, name, value))
        log.info("control %s = %r", name, stored)
        self.publish_state({name: stored})

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

    def on_session_open(self):
        if self.backgrounding.backgrounded:
            self._device_executor.submit(self.backgrounding.on_session_open)

    def on_session_close(self):
        if self.backgrounding.active:
            self._device_executor.submit(self.backgrounding.on_session_close)

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
                self.produce_still(None)
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
