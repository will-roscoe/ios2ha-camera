"""Press Guardian Eye's "Start streaming", and refuse to do anything else.

Getting the phone ready -- cable back, app running, app in front -- belongs to
the flows that already exist. This step is deliberately narrow, because the same
keys are destructive a moment either side of the right one: on a streaming app
the last item is *Stop* streaming, and on the lock screen it is the torch. So it
acts only when the app's process is running *and* its server answers 503, and it
re-reads that answer immediately before the press.

The sequence is Will's, found by hand with a real keyboard on 2026-09-15: Tab
raises the focus frame, Start streaming is the last item so enough Downs always
reach it, and Full Keyboard Access's first/last-item commands only cover what is
visible on screen.
"""

import logging
import time

log = logging.getLogger(__name__)

SEQUENCE = ["tab"] + ["down"] * 40
# Every value attempt() can return. Declared here so mqtt_io can hand the same
# list to Home Assistant, which silently drops an enum value it was not told of.
STREAM_START_STATES = ("idle", "starting", "started", "not_ready", "already_streaming",
                       "paused", "rate_limited", "failed_keyboard_not_connected",
                       "failed_keys_had_no_effect")

SETTLE_S = 1.0
SETTLE_TRIES = 10
KEYBOARD_WAIT_S = 60.0

# Bounded so a phone that cannot be fixed by keys -- Bluetooth off, app wedged --
# is not poked all night.
MAX_PER_HOUR = 6
MAX_PER_DAY = 20
MIN_GAP_S = 120.0


class StreamStarter:
    """One guarded attempt at a time, and only when it is safe to press keys.

    `probe()` answers "streaming", "off" or "unreachable"; `is_running()` says
    whether the app's process exists; `allowed()` is the pause switch.
    """

    def __init__(self, probe, keyboard, is_running, clock=time.monotonic,
                 sleep=time.sleep, allowed=lambda: True):
        self._probe = probe
        self._kb = keyboard
        self._is_running = is_running
        self._clock = clock
        self._sleep = sleep
        self._allowed = allowed
        self._attempts = []          # clock readings of attempts that spent budget
        self.state = "idle"

    @property
    def attempts_last_hour(self):
        now = self._clock()
        return sum(1 for t in self._attempts if now - t < 3600.0)

    def attempt(self, manual=False):
        """Try to start the stream. Returns a STREAM_START_STATES value.

        `manual=True` is the Home Assistant button: a person is present, so it
        ignores the pause switch and the rate limit, but none of the guards.
        """
        now = self._clock()
        if not manual and not self._allowed():
            return self._done("paused")
        if not manual and not self._budget_ok(now):
            log.info("start streaming: rate limited (%d attempts in the last hour)",
                     self.attempts_last_hour)
            return self._done("rate_limited")
        if not self._running():
            return self._done("not_ready")

        before = self._probe()
        if before == "streaming":
            return self._done("already_streaming")
        if before != "off":
            # Refused: backgrounded, locked, or not in front. Another flow's job.
            log.info("start streaming: the app is not answering on its page (%s)", before)
            return self._done("not_ready")
        if not self._wait_for_keyboard():
            return self._done("failed_keyboard_not_connected")

        self._attempts.append(now)
        self.state = "starting"
        self._kb.press(SEQUENCE)
        # Re-read, rather than trusting `before`: the Downs take about fifteen
        # seconds, and a Space sent to a streaming app would stop it.
        if self._probe() != "off":
            return self._done("already_streaming")
        self._kb.press(["space"])

        for _ in range(SETTLE_TRIES):
            self._sleep(SETTLE_S)
            if self._probe() == "streaming":
                self._attempts.clear()      # it worked, so the budget is not spent
                log.info("start streaming: the stream is up")
                return self._done("started")
        log.warning("start streaming: keys were sent but the app is still not streaming")
        return self._done("failed_keys_had_no_effect")

    # -- internals -------------------------------------------------------------

    def _done(self, result):
        self.state = result
        return result

    def _running(self):
        try:
            return bool(self._is_running())
        except Exception as e:
            # The cable answering is itself a precondition; not our failure to fix.
            log.info("start streaming: cannot tell if the app is running (%s: %s)",
                     type(e).__name__, e)
            return False

    def _budget_ok(self, now):
        self._attempts = [t for t in self._attempts if now - t < 86400.0]
        if self._attempts and now - self._attempts[-1] < MIN_GAP_S:
            return False
        if len(self._attempts) >= MAX_PER_DAY:
            return False
        return sum(1 for t in self._attempts if now - t < 3600.0) < MAX_PER_HOUR

    def _wait_for_keyboard(self):
        waited = 0.0
        while not self._kb.connected():
            if waited >= KEYBOARD_WAIT_S:
                log.warning("start streaming: no keyboard host "
                            "(is Bluetooth switched on on the phone?)")
                return False
            self._sleep(1.0)
            waited += 1.0
        return True
