"""Rebooting the phone when its developer services wedge.

A reboot is the heaviest thing this service can do to the phone, and the only
known cure for the wedge seen on 2026-09-14 and 2026-09-18: the disk image
mounter hangs and the DVT handshake is refused while plain lockdown keeps
working, and it never recovers on its own.

So every test here is one of three things: the diagnosis that must be certain
before rebooting, the budget that stops it rebooting all night, or the latch
that makes a useless reboot escalate instead of repeat.
"""

import pytest

from ios2ha_camera.reboot import (
    MAX_PER_DAY,
    MIN_GAP_S,
    REBOOT_STATES,
    SETTLE_S,
    WEDGE_FAILURES,
    WEDGE_FOR_S,
    PhoneRebooter,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Call:
    """Records calls and answers in turn; an Exception answer is raised."""

    def __init__(self, *answers):
        self.answers = list(answers) or [True]
        self.calls = 0

    def __call__(self, *args):
        self.calls += 1
        answer = self.answers[(self.calls - 1) % len(self.answers)]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def parts():
    clock = Clock()
    slept = []
    return {
        "reboot": Call(True),
        "probe": Call(True),
        "reachable": Call(True),
        "clock": clock,
        "sleep": slept.append,
        "slept": slept,
    }


def make(parts, **kw):
    return PhoneRebooter(reboot=parts["reboot"], probe=parts["probe"],
                         reachable=parts["reachable"], clock=parts["clock"],
                         sleep=parts["sleep"], **kw)


def wedge(r, clock, failures=WEDGE_FAILURES, seconds=WEDGE_FOR_S):
    """Put the rebooter in the state a real wedge produces."""
    r.observe(failures)
    clock.advance(seconds)
    r.observe(failures + 1)


# -- the diagnosis ----------------------------------------------------------

def test_does_nothing_while_dvt_is_healthy(parts):
    r = make(parts)
    r.observe(0)
    assert r.attempt() == "not_needed"
    assert parts["reboot"].calls == 0


def test_does_not_reboot_on_a_brief_failure_run(parts):
    """A handful of failures is a transient error, not a wedge."""
    r = make(parts)
    r.observe(WEDGE_FAILURES)
    parts["clock"].advance(WEDGE_FOR_S / 2)
    r.observe(WEDGE_FAILURES + 5)
    assert r.attempt() == "not_needed"
    assert parts["reboot"].calls == 0


def test_does_not_reboot_below_the_failure_threshold(parts):
    r = make(parts)
    r.observe(WEDGE_FAILURES - 1)
    parts["clock"].advance(WEDGE_FOR_S * 10)
    r.observe(WEDGE_FAILURES - 1)
    assert r.attempt() == "not_needed"
    assert parts["reboot"].calls == 0


def test_a_recovery_clears_the_wedge_timer(parts):
    """DVT working again resets the clock, so old failures cannot add up."""
    r = make(parts)
    r.observe(WEDGE_FAILURES)
    parts["clock"].advance(WEDGE_FOR_S - 1)
    r.observe(0)                       # recovered by itself
    r.observe(WEDGE_FAILURES)          # and wedged again just now
    parts["clock"].advance(10)
    r.observe(WEDGE_FAILURES)
    assert r.attempt() == "not_needed"
    assert parts["reboot"].calls == 0


def test_reboots_a_sustained_wedge(parts):
    r = make(parts)
    wedge(r, parts["clock"])
    assert r.attempt() == "recovered"
    assert parts["reboot"].calls == 1
    assert parts["slept"] == [SETTLE_S]


def test_refuses_when_the_phone_is_unreachable_at_all(parts):
    """A dead cable is not a wedge: plain lockdown must still answer.

    Rebooting on no evidence is exactly the mistake AppOpener's docstring warns
    about -- "the cable cannot tell us" is not "the phone needs a reboot".
    """
    parts["reachable"] = Call(False)
    r = make(parts)
    wedge(r, parts["clock"])
    assert r.attempt() == "unavailable"
    assert parts["reboot"].calls == 0


def test_a_raising_reachable_check_is_also_unavailable(parts):
    parts["reachable"] = Call(OSError("no such device"))
    r = make(parts)
    wedge(r, parts["clock"])
    assert r.attempt() == "unavailable"
    assert parts["reboot"].calls == 0


# -- escalation, not repetition ---------------------------------------------

def test_a_reboot_that_does_not_help_latches(parts):
    parts["probe"] = Call(False)
    r = make(parts)
    wedge(r, parts["clock"])
    assert r.attempt() == "still_wedged"
    assert parts["reboot"].calls == 1

    # Budget would allow another tomorrow, but the latch must not.
    parts["clock"].advance(MIN_GAP_S * 3)
    r.observe(WEDGE_FAILURES + 99)
    assert r.attempt() == "given_up"
    assert parts["reboot"].calls == 1


def test_a_raising_probe_counts_as_still_wedged(parts):
    parts["probe"] = Call(RuntimeError("ConnectionTerminatedError"))
    r = make(parts)
    wedge(r, parts["clock"])
    assert r.attempt() == "still_wedged"


def test_dvt_working_again_releases_the_latch(parts):
    """Only evidence clears it: a human fix, or a later spontaneous recovery."""
    parts["probe"] = Call(False, True)
    r = make(parts)
    wedge(r, parts["clock"])
    assert r.attempt() == "still_wedged"

    r.observe(0)
    assert r.latched is False
    parts["clock"].advance(MIN_GAP_S)
    wedge(r, parts["clock"])
    assert r.attempt() == "recovered"
    assert parts["reboot"].calls == 2


def test_a_failed_reboot_call_does_not_latch(parts):
    """Failing to ask is not evidence the reboot would not have worked."""
    parts["reboot"] = Call(RuntimeError("lockdown gone"))
    r = make(parts)
    wedge(r, parts["clock"])
    assert r.attempt() == "failed_reboot"
    assert r.latched is False
    assert parts["slept"] == []


# -- the budget --------------------------------------------------------------

def test_will_not_reboot_twice_in_quick_succession(parts):
    r = make(parts)
    wedge(r, parts["clock"])
    assert r.attempt() == "recovered"

    r.observe(0)
    wedge(r, parts["clock"])
    assert r.attempt() == "rate_limited"
    assert parts["reboot"].calls == 1


def test_caps_reboots_per_day(parts):
    r = make(parts)
    for _ in range(MAX_PER_DAY):
        r.observe(0)
        wedge(r, parts["clock"])
        assert r.attempt() == "recovered"
        parts["clock"].advance(MIN_GAP_S + 1)

    r.observe(0)
    wedge(r, parts["clock"])
    assert r.attempt() == "rate_limited"
    assert parts["reboot"].calls == MAX_PER_DAY


def test_the_budget_frees_up_after_a_day(parts):
    r = make(parts)
    for _ in range(MAX_PER_DAY):
        r.observe(0)
        wedge(r, parts["clock"])
        r.attempt()
        parts["clock"].advance(MIN_GAP_S + 1)

    parts["clock"].advance(86400)
    r.observe(0)
    wedge(r, parts["clock"])
    assert r.attempt() == "recovered"


def test_the_switch_pauses_it(parts):
    r = make(parts, allowed=lambda: False)
    wedge(r, parts["clock"])
    assert r.attempt() == "paused"
    assert parts["reboot"].calls == 0


# -- a person asking ---------------------------------------------------------

def test_manual_skips_the_switch_the_budget_and_the_wedge_test(parts):
    """The Home Assistant button is a person deciding, so it needs no diagnosis."""
    r = make(parts, allowed=lambda: False)
    r.observe(0)
    assert r.attempt(manual=True) == "recovered"
    assert parts["reboot"].calls == 1


def test_manual_skips_the_latch_too(parts):
    parts["probe"] = Call(False, True)
    r = make(parts)
    wedge(r, parts["clock"])
    assert r.attempt() == "still_wedged"
    assert r.attempt(manual=True) == "recovered"


def test_manual_still_refuses_an_unreachable_phone(parts):
    """Not a policy choice: there is nothing to send the request over."""
    parts["reachable"] = Call(False)
    r = make(parts)
    assert r.attempt(manual=True) == "unavailable"
    assert parts["reboot"].calls == 0


# -- reporting ---------------------------------------------------------------

def test_every_state_is_declared(parts):
    """Home Assistant publishes these as an enum, so an undeclared one is invalid."""
    r = make(parts)
    seen = {r.state}
    parts["probe"] = Call(False)
    wedge(r, parts["clock"])
    seen.add(r.attempt())
    seen.add(r.attempt())
    assert seen <= set(REBOOT_STATES)


def test_counts_reboots_in_the_last_day(parts):
    r = make(parts)
    assert r.reboots_last_day == 0
    wedge(r, parts["clock"])
    r.attempt()
    assert r.reboots_last_day == 1
    parts["clock"].advance(86401)
    assert r.reboots_last_day == 0


# -- the seam with the supervisor -------------------------------------------
#
# The rebooter can be perfect and still never fire, because what actually
# decides is whether the health tick feeds it the failure count and queues the
# attempt. That wiring is what went missing for four days in September, so it
# is tested here rather than assumed.

def _wired(dvt_failures, **kw):
    from tests.test_supervisor import FakePhone, _sup

    phone = FakePhone()
    phone.dvt_failures = dvt_failures
    clock = Clock()
    rebooter = PhoneRebooter(reboot=Call(True), probe=Call(True), reachable=Call(True),
                             clock=clock, sleep=lambda s: None, **kw)
    sup = _sup(phone=phone, rebooter=rebooter)
    queued = []
    sup._submit = lambda name, fn, *a, **k: (queued.append(name), fn())[0]
    return sup, rebooter, clock, queued


def test_the_health_tick_feeds_the_rebooter():
    sup, rebooter, clock, queued = _wired(WEDGE_FAILURES + 2)
    sup._maybe_reboot_phone()
    assert rebooter.wedged_for == 0.0          # the wedge starts now, not before
    clock.advance(WEDGE_FOR_S + 1)
    sup._maybe_reboot_phone()
    assert queued == ["reboot_phone"]
    assert rebooter.state == "recovered"


def test_the_health_tick_does_not_queue_for_a_healthy_phone():
    sup, rebooter, clock, queued = _wired(0)
    clock.advance(WEDGE_FOR_S * 10)
    sup._maybe_reboot_phone()
    assert queued == []


def test_the_state_carries_the_reboot_fields():
    sup, rebooter, _, _ = _wired(0)
    state = sup.health_state()
    assert state["phone_reboot"] == "idle"
    assert state["phone_reboots"] == 0


# -- the budget survives a restart ---------------------------------------------
#
# Every phone reboot restarts this service, through the usbmuxd hook. Measured
# 2026-09-23..28: nine reboots, each followed about a minute later by a restart
# that forgot the budget -- on the 27th two reboots ran 22 minutes apart. Will's
# ruling 2026-09-28: keep the budget across restarts, relaxed to three a day and
# twenty minutes apart, because the second of those two is the one that worked.

from ios2ha_camera.reboot import RebootHistory  # noqa: E402


def test_the_limits_are_the_ruled_ones():
    assert MAX_PER_DAY == 3
    assert MIN_GAP_S == 20 * 60


def test_the_27_september_pair_is_allowed(parts):
    """Two reboots 22 minutes apart, the second of which cured the wedge."""
    r = make(parts)
    wedge(r, parts["clock"])
    assert r.attempt() == "recovered"
    r.observe(0)
    parts["clock"].advance(22 * 60 - WEDGE_FOR_S)
    wedge(r, parts["clock"])
    assert r.attempt() == "recovered"


def test_a_reboot_is_remembered_by_the_next_process(parts, tmp_path):
    history = RebootHistory(str(tmp_path / "reboots.json"))
    first = make(parts, history=history)
    for _ in range(MAX_PER_DAY):
        first.observe(0)
        wedge(first, parts["clock"])
        assert first.attempt() == "recovered"
        parts["clock"].advance(MIN_GAP_S + 1)

    # The restart the reboot itself causes: a new rebooter, the same file.
    second = make(parts, history=RebootHistory(str(tmp_path / "reboots.json")))
    assert second.reboots_last_day == MAX_PER_DAY
    wedge(second, parts["clock"])
    assert second.attempt() == "rate_limited"


def test_a_missing_or_corrupt_history_is_an_empty_one(tmp_path, caplog):
    assert RebootHistory(str(tmp_path / "absent.json")).load() == []
    bad = tmp_path / "reboots.json"
    bad.write_text("{not json")
    with caplog.at_level("WARNING"):
        assert RebootHistory(str(bad)).load() == []
    bad.write_text('{"reboots": ["x", 5.0, null]}')
    assert RebootHistory(str(bad)).load() == [5.0]


def test_a_history_that_cannot_be_written_does_not_stop_a_reboot(parts, tmp_path):
    r = make(parts, history=RebootHistory(str(tmp_path / "missing-dir" / "reboots.json")))
    wedge(r, parts["clock"])
    assert r.attempt() == "recovered"


def test_no_verdict_while_the_service_is_stopping(parts, caplog):
    """The reboot restarts this service about a minute in, while the attempt is
    still settling. A verdict then is the dying process judging a phone it can
    no longer see: on the 26th and 27th it logged "still wedged, giving up"
    both times, and both times the next start found the phone fine."""
    r = make(parts)
    parts["probe"].answers = [False]
    r._sleep = lambda seconds: True       # the stop event, set during the settle
    wedge(r, parts["clock"])
    with caplog.at_level("INFO"):
        assert r.attempt() == "rebooting"
    assert r.latched is False
    assert not [rec for rec in caplog.records if rec.levelname == "ERROR"]
