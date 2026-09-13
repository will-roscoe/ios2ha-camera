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

import logging
import threading
import time

log = logging.getLogger(__name__)

VIEWER_LINGER_S = 30.0
TICK_WAIT_S = 0.5


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
        self.probe = probe
        self._clock = clock if clock is not None else time.time
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._counts = {}
        self._linger_until = {}
        self._open = False
        self._opened_at = None

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

    # -- session -------------------------------------------------------------

    def session_start_ts(self):
        """When the current session opened, or None. Frames older than this
        belong to an earlier session or to a probe, and a capture must not
        stack them alongside its own."""
        return self._opened_at

    def tick(self, now=None):
        now = self._clock() if now is None else now
        want = self._wanted(now)
        if want and not self._open:
            self.ing.start()
            self._open, self._opened_at = True, now
        elif not want and self._open:
            self.ing.stop()
            self._open, self._opened_at = False, None
