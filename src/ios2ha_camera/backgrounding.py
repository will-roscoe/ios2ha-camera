"""Backgrounding the camera app between captures, to save battery.

iOS suspends a backgrounded app's listening socket while leaving the process
alive, so the stream pauses and resumes without a restart and without anybody
tapping anything. Measured 2026-09-13 on the real phone: backgrounded it went
from 30% to 96% in under three hours, where streaming on the same 3 W port barely
breaks even. Verified 2026-09-14 that a background/foreground round trip keeps the
same pid and resumes the stream.

This module holds the *decision* only. It never touches usbmux itself and never
blocks: it is called from the supervisor, which runs the phone calls on its device
worker thread.

The failure worth designing against is thrash. Each transition is a usbmux round
trip and drops the WiFi radio into power save, so a policy that flipped on every
reading would cost more than it saves and make the camera unreliable. Hence a
hysteresis band rather than one threshold, and hence the state tracking that stops
it repeating a transition it has already made.
"""

import logging

log = logging.getLogger(__name__)


class BackgroundingPolicy:
    """Decides when the camera app should be backgrounded, and drives the phone.

    The thresholds and the enable switch are read through callables rather than
    copied, because all three are live Home Assistant controls that can change
    between one capture and the next.
    """

    def __init__(self, phone, enabled, below_pct, above_pct):
        self._phone = phone
        self._enabled = enabled
        self._below = below_pct
        self._above = above_pct
        # Whether the policy wants to save power, i.e. the battery is low enough
        # and the switch allows it. Not the same as `backgrounded`.
        self.active = False
        # Whether the app is actually backgrounded right now. Tracked separately
        # so a failed call leaves an honest picture: believing the app is
        # backgrounded when it is not would skip the foreground before the next
        # capture, and the still would come back empty.
        self.backgrounded = False

    # -- policy ---------------------------------------------------------------

    def on_battery(self, level):
        """Feed a battery reading. Returns whether `active` changed.

        `level` may be None when the cable cannot answer, which changes nothing:
        guessing would either strand the app backgrounded or give up the saving
        on a phone that needs it.
        """
        was = self.active
        self.active = self._decide(level)
        if self.active == was:
            return False
        log.info("backgrounding %s (battery %s%%, band %s-%s)",
                 "engaged" if self.active else "released",
                 "unknown" if level is None else round(level),
                 self._below(), self._above())
        # Releasing must undo itself. Otherwise switching the feature off, or a
        # battery that recovered, would leave the app backgrounded and the camera
        # dark until something else happened to wake it.
        if not self.active and self.backgrounded:
            self._foreground()
        return True

    def _decide(self, level):
        if self._phone is None or not self._enabled():
            return False
        below, above = self._below(), self._above()
        # Home Assistant lets both numbers be set freely, so the band can be
        # inverted or empty. Refusing to engage beats thrashing on every reading.
        if not below < above:
            log.warning("backgrounding disabled: background_below_pct (%s) must be "
                        "below resume_above_pct (%s)", below, above)
            return False
        if level is None:
            return self.active
        if level < below:
            return True
        if level > above:
            return False
        return self.active          # inside the band, hold whatever we were

    # -- driving the phone ----------------------------------------------------

    def on_session_open(self):
        """Something needs frames. Wake the app if this policy put it to sleep."""
        if self.backgrounded:
            self._foreground()

    def on_session_close(self):
        """Nothing needs frames any more. Sleep the app if the policy wants to."""
        if self.active and not self.backgrounded:
            self._background()

    def _background(self):
        try:
            self._phone.background()
        except Exception as e:
            # Leave `backgrounded` False: the app is still in the foreground, and
            # the next capture must not skip its foreground call.
            log.info("could not background the app: %s: %s", type(e).__name__, e)
            return
        self.backgrounded = True
        log.info("app backgrounded to save battery")

    def _foreground(self):
        try:
            self._phone.foreground()
        except Exception as e:
            # Stay marked backgrounded so the next attempt retries. Claiming
            # success here would leave the app asleep with nothing to wake it.
            log.warning("could not foreground the app: %s: %s", type(e).__name__, e)
            return
        self.backgrounded = False
        log.info("app foregrounded")
