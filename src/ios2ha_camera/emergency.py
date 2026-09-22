"""Sending the phone Home over the keyboard, when nothing better is left.

Will's ruling, 2026-09-22: "keyboard based backgrounding should only be used in
an emergency/when nothing else works. DVT provides way more info along with
other things, keyboard based control is effectively blind, with no context or
success/failure reporting." So this is not an alternative to `phone.background()`
-- it runs only after that has failed, and only when the battery is low enough
that leaving the app in the foreground will finish it.

That matters because backgrounding is what keeps this phone alive: on a 3 W
port, foreground streaming draws bursts of -735 mA, where backgrounded it
charges at over +200 mA. When the developer channel wedged on 2026-09-18 that
mechanism went with it and the battery fell to 4%.

The blindness is real but not total, and the difference is the whole design: a
backgrounded app's listening socket is suspended by iOS, so a refused connection
is evidence the press landed. Nothing here reports success without it. What it
genuinely cannot do is put the app *back*: that stays a developer call, because
coming back means finding the app again on a Home screen this service cannot
see. The emergency job is only to stop the drain until a reboot restores DVT.
"""

import logging
import time

log = logging.getLogger(__name__)

# Every value attempt() can return, for the Home Assistant enum.
EMERGENCY_STATES = ("idle", "not_needed", "backgrounded", "paused", "rate_limited",
                    "failed_no_keyboard", "failed_keys_had_no_effect")

# Cmd+H is Home on any hardware keyboard; Tab+H is Full Keyboard Access's own,
# documented in hid.py. Which one lands depends on the phone's accessibility
# settings, and this service deliberately knows nothing about them, so both are
# tried in turn and each is checked before moving on.
HOME_KEYS = ["cmd+h", "tab+h"]

# Long enough for iOS to suspend the socket after the app leaves the foreground.
SETTLE_S = 3.0

# Keys are cheap, but a phone that ignores them must not be typed at for ever;
# and each attempt is a probe against an app that may be mid-capture.
MIN_GAP_S = 300.0


class EmergencyBackgrounder:
    """Presses Home, and believes it only when the app stops answering.

    `probe` is the app's own answer in the words the rest of the service uses:
    "streaming", "off" (open, answering 503) or "unreachable" (refused or timed
    out). Only the last of those means backgrounded -- "off" is an app that is
    still up and still costing battery.
    """

    def __init__(self, keyboard, probe, below_pct, clock=time.monotonic,
                 sleep=time.sleep, allowed=lambda: True):
        self._kb = keyboard
        self._probe = probe
        self._below = below_pct
        self._clock = clock
        self._sleep = sleep
        self._allowed = allowed
        self._last = None
        self.state = "idle"

    def attempt(self, level):
        """Try to background the app by keyboard. `level` is the battery percent.

        Called only from the backgrounding policy, and only once a developer
        call has already failed.
        """
        if not self._allowed():
            return self._done("paused")
        # None is "the cable could not say", not "the battery is flat". Pressing
        # Home on a guess would background a healthy phone for no reason.
        if level is None or level > self._below():
            return self._done("not_needed")
        now = self._clock()
        if self._last is not None and now - self._last < MIN_GAP_S:
            return self._done("rate_limited")
        if not self._kb.connected():
            log.warning("emergency backgrounding: no keyboard, so the app cannot "
                        "be backgrounded at all while the developer channel is down")
            return self._done("failed_no_keyboard")

        self._last = now
        log.warning("emergency backgrounding: the developer channel is down and the "
                    "battery is at %.0f%%; sending Home over the keyboard", level)
        for keys in HOME_KEYS:
            try:
                self._kb.press([keys])
            except Exception as e:
                log.warning("emergency backgrounding: %r failed (%s: %s)",
                            keys, type(e).__name__, e)
                continue
            self._sleep(SETTLE_S)
            if self._backgrounded():
                log.info("emergency backgrounding: the app stopped listening after %r", keys)
                return self._done("backgrounded")
            log.info("emergency backgrounding: the app still answers after %r", keys)

        log.warning("emergency backgrounding: the app is still in the foreground "
                    "after every Home chord; the battery cannot be protected from here")
        return self._done("failed_keys_had_no_effect")

    # -- internals -------------------------------------------------------------

    def _done(self, result):
        self.state = result
        return result

    def _backgrounded(self):
        """Whether the app has stopped listening, which is what Home should cause."""
        try:
            return self._probe() == "unreachable"
        except Exception as e:
            # An error here is not evidence either way, and claiming success
            # without evidence is the one thing this must never do.
            log.info("emergency backgrounding: could not check the app (%s: %s)",
                     type(e).__name__, e)
            return False
