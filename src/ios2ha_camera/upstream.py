"""Decides *when* the phone is connected. `Ingestor` decides *how*.

The phone costs about 300 mA while a client is attached and roughly nothing
while it is streaming to nobody, so the connection is a resource to be taken
and given back rather than something to hold open for the life of the process.
Anything that needs frames takes a hold; the connection is open exactly when at
least one hold is (or was recently) held.

Every `Ingestor.start()`/`stop()`, every linger expiry and every probe happens
on this class's own thread, so no two callers can race over the connection.
Taking and releasing a hold only touches a counter, which is why `live.py` can
do it straight from the aiohttp event loop.
"""

import base64
import http.client
import logging
import threading
import time
from collections import deque

from .ingest import BOUNDARY
from .mjpeg import iter_multipart
from .ring_buffer import Frame

log = logging.getLogger(__name__)

VIEWER_LINGER_S = 30.0
TICK_WAIT_S = 0.5

PROBE_INTERVAL_S = 60.0
PROBE_BACKOFF_MAX_S = 300.0
ONLINE_IDLE_S = 150.0          # two probe intervals plus the probe's read timeout
TICK_STALE_S = 180.0
FRESH_S = 30.0                 # matches Supervisor.fresh_s: a live stream's frames are seconds old

PROBE_CONNECT_TIMEOUT_S = 5.0
PROBE_READ_TIMEOUT_S = 10.0

CAPTURE_OPEN_TIMEOUT_S = 20.0   # longer than one full connect-and-read attempt


class ProbeStreamingOff(Exception):
    """The app answered 503: it is running but not streaming."""


def http_probe(settings):
    """One frame, then close. The cheapest question that distinguishes
    'streaming', 'running but not streaming' (503) and 'gone'. Measured at
    82-387 ms and about 2-3 mA a minute."""
    auth = "Basic " + base64.b64encode(
        f"{settings.phone_user}:{settings.phone_password}".encode()).decode()
    conn = http.client.HTTPConnection(settings.phone_host, settings.phone_port,
                                      timeout=PROBE_CONNECT_TIMEOUT_S)
    try:
        conn.connect()
        # Set here, not after getresponse(): a close-delimited response (which
        # is what the app sends) makes getresponse() drop conn.sock, same as
        # ingest.py's _stream_once, leaving nothing to raise the timeout on.
        conn.sock.settimeout(PROBE_READ_TIMEOUT_S)
        conn.request("GET", "/mjpeg", headers={"Authorization": auth})
        resp = conn.getresponse()
        if resp.status == 503:
            raise ProbeStreamingOff()
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status}")
        return Frame(next(iter_multipart(resp, BOUNDARY)), time.time())
    finally:
        conn.close()


class Hold:
    """A reason the phone should be connected.

    Releasing twice is deliberately harmless: a viewer that drops mid-write can
    reach the release path more than once, and a second decrement would cancel
    somebody else's hold.
    """

    def __init__(self, upstream, reason, linger_s):
        self._upstream, self.reason, self.linger_s = upstream, reason, linger_s
        self._released = False

    def release(self):
        self._upstream._release(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class Upstream:
    def __init__(self, ingestor, buffer, settings, on_state=lambda s: None, *, probe=None, clock=None):
        self.ing = ingestor
        self.buffer = buffer
        self.s = settings
        self.on_state = on_state
        self.probe = probe if probe is not None else (lambda: http_probe(self.s))
        self._clock = clock if clock is not None else time.time
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._counts = {}
        self._linger_until = {}
        self._open = False
        self._opened_at = None
        self._failure = None            # None | "streaming_off" | "unreachable"
        self._probe_failures = 0
        self._next_probe_at = 0.0
        self._last_tick = self._clock()
        self._published = None
        self._sessions = deque()        # (opened_at, closed_at) for duty_1h
        self._stop_event = threading.Event()
        self._thread = None

    # -- holds ---------------------------------------------------------------

    def hold(self, reason, linger_s=0.0):
        with self._wake:
            self._counts[reason] = self._counts.get(reason, 0) + 1
            self._linger_until.pop(reason, None)
            self._wake.notify_all()
        return Hold(self, reason, linger_s)

    def _release(self, hold):
        with self._wake:
            # Two threads can reach release() for one hold (e.g. asyncio finally
            # racing a cancellation), so the check-and-set must be atomic under
            # the lock to avoid a double decrement that would cancel another hold.
            if hold._released:
                return
            hold._released = True
            reason, linger_s = hold.reason, hold.linger_s
            left = self._counts.get(reason, 0) - 1
            if left > 0:
                self._counts[reason] = left
            else:
                self._counts.pop(reason, None)
                if linger_s > 0:
                    now = self._clock()
                    self._linger_until[reason] = now + linger_s
            self._wake.notify_all()

    def holders(self):
        with self._lock:
            if not getattr(self.s, "on_demand", True):
                return ("on_demand_off",)
            return tuple(sorted(self._counts))

    def _wanted(self, now):
        if not getattr(self.s, "on_demand", True):
            return True
        with self._lock:
            if self._counts:
                return True
            return any(until > now for until in self._linger_until.values())

    def tick_age(self, now=None):
        return (self._clock() if now is None else now) - self._last_tick

    def state(self, now=None):
        now = self._clock() if now is None else now
        newest = self.buffer.newest_ts()
        if self._open and newest is not None and now - newest < FRESH_S:
            return "streaming"
        if self._failure is not None:
            return self._failure
        if newest is not None and now - newest < ONLINE_IDLE_S:
            return "idle"
        return "unreachable"

    def camera_online(self, now=None):
        now = self._clock() if now is None else now
        # A dead upstream thread must not leave every published value frozen at
        # its last good reading: stale ticks read as offline, which the existing
        # Home Assistant alert already watches for.
        if self.tick_age(now) > TICK_STALE_S:
            return False
        return self.state(now) in ("streaming", "idle")

    def duty_1h(self, now=None):
        now = self._clock() if now is None else now
        start = now - 3600.0
        spans = list(self._sessions)
        if self._open and self._opened_at is not None:
            spans.append((self._opened_at, now))
        total = sum(max(0.0, min(b, now) - max(a, start)) for a, b in spans)
        return round(100.0 * total / 3600.0, 1)

    def on_ingest_state(self, state):
        """`Ingestor`'s callback. Frames prove health; a failure names itself."""
        with self._wake:
            self._failure = None if state == "streaming" else state
        self._publish()

    def _publish(self, now=None):
        state = self.state(now)
        # Two threads reach here -- the tick thread and the ingest thread via
        # on_ingest_state -- so the decision to publish must be atomic, or the
        # same state change goes out twice, or out of order. The check-and-set
        # is inside the lock; on_state() runs outside because it does MQTT I/O.
        with self._wake:
            if state == self._published:
                return
            self._published = state
        self.on_state(state)

    def _run_probe(self, now):
        try:
            frame = self.probe()
        except ProbeStreamingOff:
            with self._wake:
                self._failure = "streaming_off"
                self._probe_failures += 1
        except Exception as e:
            log.info("probe failed: %s: %s", type(e).__name__, e)
            with self._wake:
                self._failure = "unreachable"
                self._probe_failures += 1
        else:
            if frame is not None:
                self.buffer.append(frame)
            with self._wake:
                self._failure = None
                self._probe_failures = 0
        # Back off after repeated failures rather than knocking on a wedged app
        # every minute for as long as it stays broken.
        delay = min(PROBE_INTERVAL_S * (2 ** (self._probe_failures - 1)), PROBE_BACKOFF_MAX_S)
        self._next_probe_at = now + (PROBE_INTERVAL_S if self._probe_failures < 2 else delay)

    # -- session -------------------------------------------------------------

    def session_start_ts(self):
        """When the current session opened, or None. Frames older than this
        belong to an earlier session or to a probe, and a capture must not
        stack them alongside its own."""
        return self._opened_at

    def tick(self, now=None):
        now = self._clock() if now is None else now
        self._last_tick = now
        want = self._wanted(now)
        if want and not self._open:
            self.ing.start()
            self._open, self._opened_at = True, now
        elif not want and self._open:
            self.ing.stop()
            self._sessions.append((self._opened_at, now))
            while self._sessions and self._sessions[0][1] < now - 3600.0:
                self._sessions.popleft()
            self._open, self._opened_at = False, None
            self._next_probe_at = now + PROBE_INTERVAL_S
        if not self._open and self.probe is not None and now >= self._next_probe_at:
            self._run_probe(now)
        self._publish(now)

    # -- thread ----------------------------------------------------------------

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="upstream", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:
                # One bad tick must never end the thread: everything the service
                # publishes about the phone comes from here, and a dead thread
                # would freeze it all at its last good value.
                log.exception("upstream tick failed")
            with self._wake:
                self._wake.wait(TICK_WAIT_S)

    def stop(self, timeout=15.0):
        self._stop_event.set()
        with self._wake:
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                # Still inside a tick, most likely a probe waiting out its timeout.
                # Touching the ingestor now would put two threads on the one
                # connection -- the thing that wedges the app -- so leave it to
                # the thread, which is a daemon and dies with the process.
                log.warning("upstream thread did not exit within %.1fs; leaving ingest to it", timeout)
                return
        if self._open:
            self.ing.stop()
            self._open, self._opened_at = False, None

    def wait_for_frames(self, n, since_ts, timeout, poll=0.1):
        """Block this (worker) thread until n frames newer than since_ts exist.

        Returns how many there are, which may be fewer on timeout. Polling
        rather than a condition because frames arrive on the ingest thread,
        which knows nothing about this class.
        """
        deadline = time.monotonic() + timeout
        while True:
            have = sum(1 for f in self.buffer.recent(n) if f.ts >= since_ts)
            if have >= n or time.monotonic() >= deadline:
                return have
            time.sleep(poll)
