"""Backgrounding the app over the keyboard when the developer channel is dead.

Will's ruling, 2026-09-22: the keyboard is an emergency route only. It is blind
-- no context, no success or failure reported -- where DVT tells the service
what actually happened, so DVT stays the primary channel and this runs only when
that has already failed and the battery is about to die.

The verification is what makes it usable at all: a backgrounded app's socket
stops listening, so a refused connection is real evidence the keypress landed.
These tests are mostly about refusing to press, and about never claiming success
without that evidence.
"""

import pytest

from ios2ha_camera.emergency import EMERGENCY_STATES, HOME_KEYS, MIN_GAP_S, EmergencyBackgrounder


class Keyboard:
    def __init__(self, connected=True, error=None):
        self._connected = connected
        self.pressed = []
        self.error = error

    def connected(self):
        return self._connected

    def press(self, keys):
        if self.error:
            raise self.error
        self.pressed.append(list(keys))


class Probe:
    """The app's own answer, in the words the rest of the service uses."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.answers[min(self.calls - 1, len(self.answers) - 1)]


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make(kb=None, probe=None, clock=None, **kw):
    return EmergencyBackgrounder(kb or Keyboard(),
                                 probe or Probe("unreachable"),
                                 clock=clock or Clock(),
                                 sleep=lambda s: None, **kw)


# -- when it refuses ---------------------------------------------------------

def test_does_nothing_above_the_emergency_floor():
    """A healthy battery is not an emergency, however dead DVT is."""
    kb = Keyboard()
    e = make(kb, below_pct=lambda: 15.0)
    assert e.attempt(60.0) == "not_needed"
    assert kb.pressed == []


def test_does_nothing_when_the_battery_is_unknown():
    """The cable could not say. Pressing Home on a guess is not an emergency."""
    kb = Keyboard()
    e = make(kb, below_pct=lambda: 15.0)
    assert e.attempt(None) == "not_needed"
    assert kb.pressed == []


def test_refuses_without_a_keyboard():
    kb = Keyboard(connected=False)
    e = make(kb, below_pct=lambda: 15.0)
    assert e.attempt(5.0) == "failed_no_keyboard"
    assert kb.pressed == []


def test_the_switch_pauses_it():
    kb = Keyboard()
    e = make(kb, below_pct=lambda: 15.0, allowed=lambda: False)
    assert e.attempt(5.0) == "paused"
    assert kb.pressed == []


# -- pressing, and proving it worked ----------------------------------------

def test_presses_home_and_verifies_the_app_stopped_listening():
    kb = Keyboard()
    # Refused: iOS suspends a backgrounded app's socket, so this is the evidence.
    e = make(kb, Probe("unreachable"), below_pct=lambda: 15.0)
    assert e.attempt(5.0) == "backgrounded"
    assert kb.pressed == [[HOME_KEYS[0]]]


def test_tries_the_second_home_chord_when_the_first_does_nothing():
    """Cmd+H is the hardware-keyboard Home; Tab+H is Full Keyboard Access's.
    Which one lands depends on the phone's settings, so both are tried."""
    kb = Keyboard()
    e = make(kb, Probe("streaming", "unreachable"), below_pct=lambda: 15.0)
    assert e.attempt(5.0) == "backgrounded"
    assert kb.pressed == [[HOME_KEYS[0]], [HOME_KEYS[1]]]


def test_reports_failure_when_the_app_keeps_answering():
    """No claim without evidence: the app still listening means it is still up."""
    kb = Keyboard()
    e = make(kb, Probe("streaming"), below_pct=lambda: 15.0)
    assert e.attempt(5.0) == "failed_keys_had_no_effect"
    assert len(kb.pressed) == len(HOME_KEYS)


def test_an_app_answering_503_is_also_still_in_the_foreground():
    """`off` means the server answered, so the app is open and not backgrounded."""
    kb = Keyboard()
    e = make(kb, Probe("off"), below_pct=lambda: 15.0)
    assert e.attempt(5.0) == "failed_keys_had_no_effect"


def test_a_keyboard_that_raises_is_a_failure_not_a_crash():
    kb = Keyboard(error=RuntimeError("bluez gone"))
    e = make(kb, below_pct=lambda: 15.0)
    assert e.attempt(5.0) == "failed_keys_had_no_effect"


# -- not hammering the phone -------------------------------------------------

def test_waits_between_attempts():
    kb = Keyboard()
    clock = Clock()
    e = make(kb, Probe("streaming"), clock=clock, below_pct=lambda: 15.0)
    assert e.attempt(5.0) == "failed_keys_had_no_effect"
    assert e.attempt(5.0) == "rate_limited"

    clock.advance(MIN_GAP_S + 1)
    assert e.attempt(5.0) == "failed_keys_had_no_effect"


def test_a_success_does_not_bar_a_later_emergency():
    """The app can come back to the foreground on its own, and then the battery
    is draining again -- so a success must not lock the door behind it."""
    kb = Keyboard()
    clock = Clock()
    e = make(kb, Probe("unreachable"), clock=clock, below_pct=lambda: 15.0)
    assert e.attempt(5.0) == "backgrounded"
    clock.advance(MIN_GAP_S + 1)
    assert e.attempt(5.0) == "backgrounded"


def test_every_state_is_declared():
    kb = Keyboard()
    e = make(kb, Probe("streaming"), below_pct=lambda: 15.0)
    seen = {e.state, e.attempt(60.0), e.attempt(5.0), e.attempt(5.0)}
    assert seen <= set(EMERGENCY_STATES)


# -- the policy only reaches for it when DVT has failed ----------------------

def test_the_policy_uses_it_only_after_the_developer_call_fails():
    from ios2ha_camera.backgrounding import BackgroundingPolicy

    class Phone:
        def __init__(self):
            self.calls = 0

        def background(self):
            self.calls += 1
            raise RuntimeError("ConnectionTerminatedError")

    class Emergency:
        state = "idle"

        def __init__(self, answer):
            self.answer = answer
            self.levels = []

        def attempt(self, level):
            self.levels.append(level)
            return self.answer

    phone, emg = Phone(), Emergency("backgrounded")
    p = BackgroundingPolicy(phone, lambda: True, lambda: 50.0, lambda: 80.0,
                            emergency=emg)
    p.on_battery(5.0)
    p.on_session_close()
    assert phone.calls == 1                 # DVT first, always
    assert emg.levels == [5.0]              # then the emergency, with the level
    assert p.backgrounded is True
    assert p.backgrounded_by_keyboard is True


def test_the_policy_stays_honest_when_the_emergency_fails_too():
    from ios2ha_camera.backgrounding import BackgroundingPolicy

    class Phone:
        def background(self):
            raise RuntimeError("ConnectionTerminatedError")

    class Emergency:
        state = "idle"

        def attempt(self, level):
            return "failed_keys_had_no_effect"

    p = BackgroundingPolicy(Phone(), lambda: True, lambda: 50.0, lambda: 80.0,
                            emergency=Emergency())
    p.on_battery(5.0)
    p.on_session_close()
    assert p.backgrounded is False
    assert p.backgrounded_by_keyboard is False


def test_a_working_developer_call_never_reaches_the_keyboard():
    from ios2ha_camera.backgrounding import BackgroundingPolicy

    class Phone:
        def background(self):
            return True

    class Emergency:
        state = "idle"

        def attempt(self, level):
            raise AssertionError("the keyboard must not be used while DVT works")

    p = BackgroundingPolicy(Phone(), lambda: True, lambda: 50.0, lambda: 80.0,
                            emergency=Emergency())
    p.on_battery(5.0)
    p.on_session_close()
    assert p.backgrounded is True
    assert p.backgrounded_by_keyboard is False


@pytest.mark.parametrize("keys", HOME_KEYS)
def test_the_home_chords_are_real_reports(keys):
    """A name hid.report cannot build would be accepted and silently do nothing."""
    from ios2ha_camera import hid

    assert len(hid.report(keys)) == 8
