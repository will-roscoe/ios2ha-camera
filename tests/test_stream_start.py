"""The guarded step that presses Guardian Eye's "Start streaming".

The guards are the point of this module: the same keys sent a moment later stop
the stream instead of starting it, and sent to a locked phone they toggle the
torch. Every test here is either a guard or the rate limit that keeps a failing
phone from being poked all night.
"""

import pytest

from ios2ha_camera.stream_start import LOCK, SEQUENCE, STREAM_START_STATES, WAKE, StreamStarter


class FakeKeyboard:
    def __init__(self, connected=True):
        self._connected = connected
        self.pressed = []

    def connected(self):
        return self._connected

    def press(self, keys):
        self.pressed.append(list(keys))
        return self._connected


class Probe:
    """Answers in turn, then repeats the last answer."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.answers[min(self.calls - 1, len(self.answers) - 1)]


def _starter(probe, keyboard=None, running=True, allowed=lambda: True):
    now = [1000.0]
    starter = StreamStarter(probe, keyboard or FakeKeyboard(), lambda: running,
                            clock=lambda: now[0], sleep=lambda s: None, allowed=allowed)
    return starter, now


def test_it_presses_the_sequence_and_reports_started():
    kb = FakeKeyboard()
    s, _ = _starter(Probe("off", "off", "streaming"), kb)
    assert s.attempt() == "started"
    assert kb.pressed == [SEQUENCE, ["space"]]


def test_the_sequence_is_tab_then_downs_then_nothing_else():
    """Will's recipe: Tab raises the focus frame, and Start streaming is the
    last item, so enough Downs always reach it."""
    assert SEQUENCE[0] == "tab"
    assert set(SEQUENCE[1:]) == {"down"}
    assert len(SEQUENCE) >= 20


def test_it_does_nothing_when_the_app_is_not_running():
    kb = FakeKeyboard()
    s, _ = _starter(Probe("off"), kb, running=False)
    assert s.attempt() == "not_ready"
    assert kb.pressed == []


def test_a_refusal_is_not_this_steps_job():
    """Refused means backgrounded, locked or not in front. Getting the app open
    is the other process; keys here would land on the lock screen, where Space
    is the torch."""
    kb = FakeKeyboard()
    s, _ = _starter(Probe("unreachable"), kb)
    assert s.attempt() == "not_ready"
    assert kb.pressed == []


def test_it_never_presses_space_when_already_streaming():
    """On a streaming app the last item is Stop streaming."""
    kb = FakeKeyboard()
    s, _ = _starter(Probe("streaming"), kb)
    assert s.attempt() == "already_streaming"
    assert kb.pressed == []


def test_space_is_withheld_when_the_state_changed_during_the_downs():
    """The guard that matters most: the probe is re-read immediately before
    Space, so a stream that came up during the ~15 s of Downs is not stopped."""
    kb = FakeKeyboard()
    s, _ = _starter(Probe("off", "streaming"), kb)
    assert s.attempt() == "already_streaming"
    assert kb.pressed == [SEQUENCE]


def test_no_keys_when_the_keyboard_has_no_host():
    kb = FakeKeyboard(connected=False)
    s, _ = _starter(Probe("off"), kb)
    assert s.attempt() == "failed_keyboard_not_connected"
    assert kb.pressed == []


def test_keys_that_do_not_start_the_stream_are_reported():
    kb = FakeKeyboard()
    s, _ = _starter(Probe("off"), kb)
    assert s.attempt() == "failed_keys_had_no_effect"
    assert kb.pressed == [SEQUENCE, ["space"]]


class Refresher:
    """Stands in for locking the phone and bringing the app back."""

    def __init__(self, fails=False):
        self.calls = 0
        self.fails = fails

    def __call__(self):
        self.calls += 1
        if self.fails:
            raise RuntimeError("the cable went away")


def _refreshing(probe, kb, foreground):
    return StreamStarter(probe, kb, lambda: True, clock=lambda: 1000.0,
                         sleep=lambda s: None, foreground=foreground)


def test_a_press_that_does_nothing_locks_the_phone_and_tries_again():
    """Measured on the device 2026-09-16: a cold-started app can come up
    ignoring the keyboard entirely, and locking and returning to it is what
    brings Full Keyboard Access back."""
    kb = FakeKeyboard()
    fg = Refresher()

    def probe():
        # Nothing works until the app has been left and come back to, and then
        # only once the second run of the sequence has reached Space.
        done = kb.pressed.count(SEQUENCE) >= 2 and kb.pressed[-1] == ["space"]
        return "streaming" if fg.calls and done else "off"

    s = _refreshing(probe, kb, fg)
    assert s.attempt() == "started"
    assert kb.pressed == [SEQUENCE, ["space"], LOCK, WAKE, SEQUENCE, ["space"]]
    assert fg.calls == 1


def test_the_phone_is_locked_at_most_once_an_attempt():
    """Otherwise a phone that will never take keys is locked and unlocked in a
    loop for as long as the budget allows."""
    kb = FakeKeyboard()
    fg = Refresher()
    s = _refreshing(Probe("off"), kb, fg)
    assert s.attempt() == "failed_keys_had_no_effect"
    assert kb.pressed.count(SEQUENCE) == 2
    assert fg.calls == 1


def test_nothing_is_locked_when_the_first_press_worked():
    kb = FakeKeyboard()
    fg = Refresher()
    s = _refreshing(Probe("off", "off", "streaming"), kb, fg)
    assert s.attempt() == "started"
    assert LOCK not in kb.pressed
    assert fg.calls == 0


def test_a_stream_that_came_up_late_is_not_locked_out_of_it():
    """The app answering "streaming" after the settle loop gave up means the
    keys did work; locking it then would be gratuitous."""
    kb = FakeKeyboard()
    fg = Refresher()
    answers = ["off"] * (2 + 10) + ["streaming"]
    s = _refreshing(Probe(*answers), kb, fg)
    assert s.attempt() == "already_streaming"
    assert LOCK not in kb.pressed
    assert fg.calls == 0


def test_no_refresh_is_attempted_without_a_way_back_into_the_app():
    """Locking a phone we then cannot unlock would leave it worse than found."""
    kb = FakeKeyboard()
    s, _ = _starter(Probe("off"), kb)
    assert s.attempt() == "failed_keys_had_no_effect"
    assert kb.pressed == [SEQUENCE, ["space"]]


def test_the_keys_are_not_pressed_again_when_the_app_did_not_come_back():
    """A Space with the app not in front is a key sent to whatever is, so the
    retry only happens once the app is confirmed back and still idle."""
    kb = FakeKeyboard()
    fg = Refresher(fails=True)
    s = _refreshing(Probe("off"), kb, fg)
    assert s.attempt() == "failed_keys_had_no_effect"
    assert kb.pressed == [SEQUENCE, ["space"], LOCK, WAKE]


def test_the_retry_is_dropped_when_the_app_is_back_but_no_longer_idle():
    kb = FakeKeyboard()
    fg = Refresher()
    # off through the first press, then unreachable once it is back.
    answers = ["off"] * (2 + 10) + ["off", "unreachable"]
    s = _refreshing(Probe(*answers), kb, fg)
    assert s.attempt() == "failed_keys_had_no_effect"
    assert kb.pressed == [SEQUENCE, ["space"], LOCK, WAKE]


def test_a_stream_found_up_after_the_refresh_is_reported_honestly():
    """The first press may have worked late, or the wake key may have landed on
    the button. Either way the stream is up, and saying it failed would have
    the supervisor try again and press Stop."""
    kb = FakeKeyboard()
    fg = Refresher()
    answers = ["off"] * (2 + 10) + ["off", "streaming"]
    s = _refreshing(Probe(*answers), kb, fg)
    assert s.attempt() == "already_streaming"
    assert kb.pressed == [SEQUENCE, ["space"], LOCK, WAKE]


def test_the_wake_key_cannot_activate_whatever_is_focused():
    """It is sent on the assumption the phone locked. If the lock chord missed,
    the app is still in front and Space would press the focused control."""
    assert WAKE == ["escape"]


def test_the_pause_switch_stops_it_before_anything_is_touched():
    kb = FakeKeyboard()
    s, _ = _starter(Probe("off"), kb, allowed=lambda: False)
    assert s.attempt() == "paused"
    assert kb.pressed == []


def test_the_button_overrides_the_pause_switch():
    kb = FakeKeyboard()
    s, _ = _starter(Probe("off", "off", "streaming"), kb, allowed=lambda: False)
    assert s.attempt(manual=True) == "started"
    assert kb.pressed == [SEQUENCE, ["space"]]


def _clocked(probe, kb, now):
    return StreamStarter(probe, kb, lambda: True, clock=lambda: now[0],
                         sleep=lambda s: None, allowed=lambda: True)


def test_attempts_are_rate_limited_per_hour():
    """Bounded on purpose: a phone with Bluetooth off must not be poked all night."""
    now = [1000.0]
    s = _clocked(Probe("off"), FakeKeyboard(), now)
    for _ in range(6):
        assert s.attempt() == "failed_keys_had_no_effect"
        now[0] += 121
    assert s.attempt() == "rate_limited"
    now[0] += 3601
    assert s.attempt() == "failed_keys_had_no_effect"


def test_two_attempts_cannot_run_within_two_minutes():
    now = [1000.0]
    s = _clocked(Probe("off"), FakeKeyboard(), now)
    assert s.attempt() == "failed_keys_had_no_effect"
    now[0] += 30
    assert s.attempt() == "rate_limited"


def test_a_successful_start_clears_the_budget():
    now = [1000.0]
    s = _clocked(Probe("off", "off", "streaming"), FakeKeyboard(), now)
    assert s.attempt() == "started"
    assert s.attempts_last_hour == 0


def test_the_manual_button_ignores_the_rate_limit():
    now = [1000.0]
    s = _clocked(Probe("off"), FakeKeyboard(), now)
    for _ in range(6):
        s.attempt()
        now[0] += 121
    assert s.attempt() == "rate_limited"
    assert s.attempt(manual=True) == "failed_keys_had_no_effect"


def test_a_day_has_its_own_cap():
    """Twenty a day, so a night of failures cannot become hundreds of presses.

    800 s apart: under the hourly cap (at most five in any hour) and all thirty
    inside one day, so it is the daily cap being measured and nothing else.
    """
    now = [1000.0]
    s = _clocked(Probe("off"), FakeKeyboard(), now)
    results = []
    for _ in range(30):
        results.append(s.attempt())
        now[0] += 800
    assert results.count("failed_keys_had_no_effect") == 20
    assert results[-1] == "rate_limited"


@pytest.mark.parametrize("answers,expected", [
    (("off", "off", "streaming"), "started"),
    (("off", "off", "off"), "failed_keys_had_no_effect"),
    (("streaming",), "already_streaming"),
    (("unreachable",), "not_ready"),
])
def test_state_records_the_last_result(answers, expected):
    s, _ = _starter(Probe(*answers))
    assert s.attempt() == expected
    assert s.state == expected


def test_every_result_is_a_declared_state():
    """Home Assistant silently drops an enum value it was not told about."""
    seen = set()
    for answers, running, allowed in (
        (("off", "off", "streaming"), True, lambda: True),
        (("off", "off", "off"), True, lambda: True),
        (("streaming",), True, lambda: True),
        (("unreachable",), True, lambda: True),
        (("off",), False, lambda: True),
        (("off",), True, lambda: False),
    ):
        s, _ = _starter(Probe(*answers), running=running, allowed=allowed)
        seen.add(s.attempt())
    s, _ = _starter(Probe("off"), FakeKeyboard(connected=False))
    seen.add(s.attempt())
    assert seen <= set(STREAM_START_STATES), seen - set(STREAM_START_STATES)
    assert "idle" in STREAM_START_STATES and "rate_limited" in STREAM_START_STATES
