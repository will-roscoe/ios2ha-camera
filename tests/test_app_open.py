"""The guarded step that cold starts Guardian Eye when it is not running.

A cold start kills the running copy, so every test here is either the guard
that stops it touching a live app or the budget that stops it relaunching a
crashing one all night.
"""

import pytest

from ios2ha_camera.app_open import APP_OPEN_STATES, AppOpener


class Running:
    """Answers in turn and then starts the cycle again, so `False, True` is an
    app that dies again after each start. An Exception answer is raised."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        answer = self.answers[(self.calls - 1) % len(self.answers)]
        if isinstance(answer, Exception):
            raise answer
        return answer


class Launch:
    def __init__(self, error=None):
        self.calls = 0
        self.error = error

    def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return True


def _opener(running, launch=None, allowed=lambda: True, now=None):
    now = now or [1000.0]
    launch = launch or Launch()
    opener = AppOpener(running, launch, clock=lambda: now[0], sleep=lambda s: None,
                       allowed=allowed)
    return opener, launch, now


def test_an_app_that_is_gone_is_cold_started():
    o, launch, _ = _opener(Running(False, True))
    assert o.attempt() == "opened"
    assert launch.calls == 1


def test_a_running_app_is_left_alone():
    """The guard that matters: a cold start kills the running copy, so an app
    that is merely suspended would lose the stream it is holding."""
    o, launch, _ = _opener(Running(True))
    assert o.attempt() == "not_needed"
    assert launch.calls == 0


def test_a_cable_that_cannot_answer_is_not_a_dead_app():
    """Over the cable there are three answers, not two. A usbmux failure means
    we do not know, and launching on "do not know" is how a live stream dies."""
    o, launch, _ = _opener(Running(RuntimeError("no usbmux")))
    assert o.attempt() == "unavailable"
    assert launch.calls == 0


def test_a_launch_that_does_not_bring_the_app_up_is_reported():
    o, launch, _ = _opener(Running(False, False))
    assert o.attempt() == "failed_launch"
    assert launch.calls == 1


def test_a_launch_that_raises_is_caught():
    o, launch, _ = _opener(Running(False), Launch(error=RuntimeError("no developer image")))
    assert o.attempt() == "failed_launch"
    assert launch.calls == 1


def test_the_pause_switch_stops_it_before_the_cable_is_touched():
    running = Running(False, True)
    o, launch, _ = _opener(running, allowed=lambda: False)
    assert o.attempt() == "paused"
    assert launch.calls == 0
    assert running.calls == 0


def test_a_person_asking_overrides_the_pause_switch_but_not_the_guard():
    o, launch, _ = _opener(Running(True), allowed=lambda: False)
    assert o.attempt(manual=True) == "not_needed"
    assert launch.calls == 0


def test_starts_are_spaced_out():
    now = [1000.0]
    o, launch, _ = _opener(Running(False, False), now=now)
    assert o.attempt() == "failed_launch"
    now[0] += 60
    assert o.attempt() == "rate_limited"
    assert launch.calls == 1


def test_an_app_that_keeps_dying_is_not_relaunched_all_night():
    now = [1000.0]
    o, launch, _ = _opener(Running(False, False), now=now)
    for _ in range(3):
        assert o.attempt() == "failed_launch"
        now[0] += 601
    assert o.attempt() == "rate_limited"
    assert launch.calls == 3


def test_success_does_not_refund_the_budget():
    """An app that dies the moment it is opened would otherwise be relaunched
    for ever, each attempt reporting success."""
    now = [1000.0]
    o, launch, _ = _opener(Running(False, True), now=now)
    for _ in range(3):
        assert o.attempt() == "opened"
        now[0] += 601
    assert o.attempt() == "rate_limited"
    assert launch.calls == 3


def test_the_hourly_cap_lifts_after_an_hour():
    now = [1000.0]
    o, launch, _ = _opener(Running(False, True), now=now)
    for _ in range(3):
        o.attempt()
        now[0] += 601
    assert o.attempt() == "rate_limited"
    now[0] += 3600
    assert o.attempt() == "opened"


def test_the_day_has_its_own_cap():
    now = [1000.0]
    o, _, _ = _opener(Running(False, True), now=now)
    for _ in range(12):
        assert o.attempt() == "opened"
        now[0] += 3601        # clear of both the gap and the hourly cap
    assert o.attempt() == "rate_limited"


def test_the_button_ignores_the_rate_limit():
    now = [1000.0]
    o, launch, _ = _opener(Running(False, True), now=now)
    o.attempt()
    now[0] += 60
    assert o.attempt() == "rate_limited"
    assert o.attempt(manual=True) == "opened"
    assert launch.calls == 2


@pytest.mark.parametrize("answers,expected", [
    ((True,), "not_needed"),
    ((False, True), "opened"),
    ((False, False), "failed_launch"),
])
def test_state_records_the_last_result(answers, expected):
    o, _, _ = _opener(Running(*answers))
    assert o.attempt() == expected
    assert o.state == expected


def test_every_result_is_a_declared_state():
    """Home Assistant silently drops an enum value it was not told about."""
    now = [1000.0]
    seen = set()
    for answers, allowed in [((True,), lambda: True), ((False, True), lambda: True),
                             ((False, False), lambda: True), ((True,), lambda: False),
                             ((RuntimeError("x"),), lambda: True)]:
        o, _, _ = _opener(Running(*answers), allowed=allowed, now=now)
        seen.add(o.attempt())
        seen.add(o.state)
    o, _, _ = _opener(Running(False, True), now=now)
    o.attempt()
    seen.add(o.attempt())          # the second one inside the gap: rate_limited
    assert seen <= set(APP_OPEN_STATES)
    assert "idle" in APP_OPEN_STATES
