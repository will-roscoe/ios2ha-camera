"""Rebooting the phone when its developer services wedge, and only then.

Twice now -- 2026-09-14 and 2026-09-18 -- the phone's DeveloperDiskImage mounter
has hung and its DVT handshake has been refused while plain lockdown carried on
answering. Nothing in software recovers it: clearing the mount cache cannot help
when the mounter itself is what hangs, and a physical cable replug did not clear
it either. Rebooting the phone does, in about six minutes, after which the
existing steps re-open the app and streaming resumes unattended.

What made the 2026-09-18 outage cost four days was not the wedge. It was that
nothing acted on it: backgrounding is what keeps the phone alive on a 3 W port,
backgrounding goes over DVT, and so the camera went dark and the battery fell to
4% with the failure counter climbing and nobody watching it.

The danger here is the opposite one, and it governs the whole design: a reboot
loop that fixes nothing, in the middle of the night, invisibly. So this acts
only on a wedge that is *certain* -- sustained, and with the phone proven
reachable by a service that does not need the developer image -- it spends a
budget whether or not it works, and a reboot that does not cure the wedge
latches instead of repeating. The latch opens on evidence only: DVT seen working
again, or a person pressing the button.
"""

import logging
import time

log = logging.getLogger(__name__)

# Every value attempt() can return, for the Home Assistant enum.
REBOOT_STATES = ("idle", "not_needed", "rebooting", "recovered", "still_wedged",
                 "given_up", "rate_limited", "paused", "unavailable",
                 "failed_reboot")

# How many consecutive developer-call failures before this looks like a wedge at
# all. `dvt_failures` resets on any success, so a run this long is already
# several failed ticks rather than one unlucky call.
WEDGE_FAILURES = 3

# ...and how long it must persist. The 2026-09-14 outage lasted 24 hours and the
# 2026-09-18 one four days, so waiting fifteen minutes costs nothing against a
# real wedge while ruling out a transient ConnectionTerminatedError.
WEDGE_FOR_S = 900.0

# Long enough for the phone to boot and for the developer services to come back:
# measured 2026-09-22, DVT answered again within 90 s of the restart request.
SETTLE_S = 120.0

# Deliberately much tighter than the other guarded steps. A reboot interrupts
# everything the phone is doing, and if two in a day have not fixed it, a third
# will not either.
#
# The gap is six hours rather than twelve so that the daily cap is the binding
# guard: at twelve, two gaps already exceed a day, so the cap could never fire
# and only one of the two numbers would mean anything. Six lets a phone that
# wedges, recovers and wedges again be rescued the same day while still holding
# the hard ceiling of two.
MIN_GAP_S = 21600.0      # 6 hours
MAX_PER_DAY = 2


class PhoneRebooter:
    """One guarded reboot at a time, and only for a phone that is truly wedged.

    `reboot()` asks the phone to restart over plain lockdown -- deliberately not
    a developer-image call, since those are exactly what is broken. `probe()`
    answers whether DVT works, `reachable()` whether the phone answers at all,
    and `allowed()` is the pause switch.
    """

    def __init__(self, reboot, probe, reachable, clock=time.monotonic,
                 sleep=time.sleep, allowed=lambda: True):
        self._reboot = reboot
        self._probe = probe
        self._reachable = reachable
        self._clock = clock
        self._sleep = sleep
        self._allowed = allowed
        self._reboots = []
        # When the current run of developer failures started, or None when DVT
        # is healthy. Time, not just a count: a burst of failures in one tick is
        # not the same fault as a channel that has been dead for a quarter hour.
        self._wedged_since = None
        # Set when a reboot did not cure the wedge. While it is set this step
        # does nothing, so a reboot that does not work is escalated to the
        # offline alert rather than repeated.
        self.latched = False
        self.state = "idle"

    @property
    def reboots_last_day(self):
        now = self._clock()
        return sum(1 for t in self._reboots if now - t < 86400.0)

    @property
    def wedged_for(self):
        """Seconds the developer channel has been failing, or 0.0 when healthy."""
        if self._wedged_since is None:
            return 0.0
        return self._clock() - self._wedged_since

    def observe(self, dvt_failures):
        """Feed the current consecutive-failure count, once per health tick."""
        if dvt_failures < WEDGE_FAILURES:
            # Any success resets the counter, so this is DVT working. That is
            # the only evidence that opens the latch: it says the fault this
            # step exists to fix is genuinely gone.
            if self.latched:
                log.info("phone reboot: the developer channel is working again")
            self._wedged_since = None
            self.latched = False
            return
        if self._wedged_since is None:
            self._wedged_since = self._clock()
            log.info("phone reboot: the developer channel is failing (%d in a row)",
                     dvt_failures)

    def attempt(self, manual=False):
        """Reboot the phone if its developer services are certainly wedged.

        `manual=True` is a person pressing the button, which skips the switch,
        the budget, the latch and the wedge test -- they can see what this step
        cannot. It keeps the reachability guard, which is not a policy but a
        precondition: an unreachable phone has nothing to send the request over.
        """
        now = self._clock()
        if not manual:
            if not self._allowed():
                return self._done("paused")
            if self.latched:
                return self._done("given_up")
            if self.wedged_for < WEDGE_FOR_S:
                return self._done("not_needed")
            if not self._budget_ok(now):
                log.info("phone reboot: rate limited (%d in the last day)",
                         self.reboots_last_day)
                return self._done("rate_limited")

        if not self._reachable_now():
            log.info("phone reboot: the phone does not answer at all, so this is "
                     "not a wedged developer channel; not rebooting")
            return self._done("unavailable")

        self._reboots.append(now)
        self.state = "rebooting"
        log.warning("phone reboot: rebooting the phone to clear its wedged "
                    "developer services (failing for %.0f s)", self.wedged_for)
        try:
            self._reboot()
        except Exception as e:
            # Never latched: failing to ask is no evidence about whether the
            # reboot would have helped.
            log.warning("phone reboot: the restart request failed (%s: %s)",
                        type(e).__name__, e)
            return self._done("failed_reboot")

        self._sleep(SETTLE_S)
        if self._dvt_ok():
            log.info("phone reboot: the developer channel is back")
            self._wedged_since = None
            return self._done("recovered")

        self.latched = True
        log.error("phone reboot: still wedged after a reboot; giving up until the "
                  "developer channel is seen working again")
        return self._done("still_wedged")

    # -- internals -------------------------------------------------------------

    def _done(self, result):
        self.state = result
        return result

    def _reachable_now(self):
        try:
            return bool(self._reachable())
        except Exception as e:
            log.info("phone reboot: cannot reach the phone (%s: %s)",
                     type(e).__name__, e)
            return False

    def _dvt_ok(self):
        try:
            return bool(self._probe())
        except Exception as e:
            log.info("phone reboot: the developer channel still fails (%s: %s)",
                     type(e).__name__, e)
            return False

    def _budget_ok(self, now):
        # A success does not refund the budget, for AppOpener's reason: a phone
        # that wedges again an hour after every reboot must not be rebooted
        # around the clock, each attempt "succeeding".
        self._reboots = [t for t in self._reboots if now - t < 86400.0]
        if self._reboots and now - self._reboots[-1] < MIN_GAP_S:
            return False
        return len(self._reboots) < MAX_PER_DAY
