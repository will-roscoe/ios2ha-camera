"""Open Guardian Eye when the phone says it is not running at all.

The step before `stream_start`, and deliberately a separate one: pressing keys
needs an app that is open and idle, and nothing else in this service will open
it. Until this existed, a phone that had rebooted sat with no app running and
the start step correctly refusing, for as long as it took someone to notice.

The danger here is the opposite of the start step's. A cold start *kills the
running copy*, so launching an app that is merely suspended throws away a live
stream, and launching one that crashes on start gives an endless relaunch loop.
So it acts only on a definite "not running" answer over the cable, re-read
immediately beforehand, and spends a small budget whether or not it works.
"""

import logging
import time

log = logging.getLogger(__name__)

# Every value attempt() can return, for the Home Assistant enum.
APP_OPEN_STATES = ("idle", "opening", "opened", "not_needed", "rate_limited",
                   "paused", "failed_launch", "unavailable")

# How long to give the app before asking whether it came up.
SETTLE_S = 5.0

# Tighter than the start step's: a cold start is destructive, and an app that
# will not stay up must not be relaunched all night.
MAX_PER_HOUR = 3
MAX_PER_DAY = 12
MIN_GAP_S = 600.0


class AppOpener:
    """One guarded cold start at a time, and only for an app that is gone.

    `is_running()` answers over the cable and may raise, which is a third
    answer: "the cable cannot tell us", not "the app is dead". `launch()` cold
    starts it. `allowed()` is the pause switch.
    """

    def __init__(self, is_running, launch, clock=time.monotonic, sleep=time.sleep,
                 allowed=lambda: True):
        self._is_running = is_running
        self._launch = launch
        self._clock = clock
        self._sleep = sleep
        self._allowed = allowed
        self._attempts = []
        self.state = "idle"

    @property
    def attempts_last_hour(self):
        now = self._clock()
        return sum(1 for t in self._attempts if now - t < 3600.0)

    def attempt(self, manual=False):
        """Cold start the app if it is definitely not running.

        `manual=True` is a person asking, which skips the pause switch and the
        rate limit but none of the guards: the app is still only launched on a
        definite "not running".
        """
        now = self._clock()
        if not manual and not self._allowed():
            return self._done("paused")
        if not manual and not self._budget_ok(now):
            log.info("open app: rate limited (%d starts in the last hour)",
                     self.attempts_last_hour)
            return self._done("rate_limited")

        running = self._running()
        if running is None:
            return self._done("unavailable")
        if running:
            # Suspended counts as running: iOS wakes it, and a cold start here
            # would throw away a stream that is merely paused.
            return self._done("not_needed")

        self._attempts.append(now)
        self.state = "opening"
        log.info("open app: the app is not running; cold starting it")
        try:
            self._launch()
        except Exception as e:
            log.warning("open app: the launch failed (%s: %s)", type(e).__name__, e)
            return self._done("failed_launch")

        self._sleep(SETTLE_S)
        if self._running() is True:
            log.info("open app: the app is up")
            return self._done("opened")
        log.warning("open app: launched, but the app is still not running")
        return self._done("failed_launch")

    # -- internals -------------------------------------------------------------

    def _done(self, result):
        self.state = result
        return result

    def _running(self):
        """True, False, or None when the cable cannot answer."""
        try:
            return bool(self._is_running())
        except Exception as e:
            log.info("open app: cannot tell if the app is running (%s: %s)",
                     type(e).__name__, e)
            return None

    def _budget_ok(self, now):
        # Unlike the start step, a success does not refund the budget: an app
        # that dies as soon as it is opened would otherwise be relaunched for
        # ever, each attempt "succeeding".
        self._attempts = [t for t in self._attempts if now - t < 86400.0]
        if self._attempts and now - self._attempts[-1] < MIN_GAP_S:
            return False
        if len(self._attempts) >= MAX_PER_DAY:
            return False
        return sum(1 for t in self._attempts if now - t < 3600.0) < MAX_PER_HOUR
