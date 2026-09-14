"""2.1.0: backgrounding the camera app between captures to save battery.

Worth roughly 370 mA. Measured 2026-09-13: backgrounded, the phone went from 30%
to 96% in under three hours, where streaming on the same 3 W port barely breaks
even. Verified 2026-09-14 that a background/foreground round trip keeps the same
process and resumes the stream with no tap.

The risk is thrash. Backgrounding costs a usbmux round trip each way and drops the
WiFi radio into power save, so a policy that flips on every reading would spend
more than it saves and would make the camera unreliable. Most of what is tested
here is therefore when it must *not* act.
"""

import pytest

from ios2ha_camera.backgrounding import BackgroundingPolicy


class Phone:
    """Records what the policy asked the phone to do."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    def foreground(self):
        self.calls.append("foreground")
        if self.fail:
            raise self.fail
        return True

    def background(self):
        self.calls.append("background")
        if self.fail:
            raise self.fail
        return True


def _policy(phone=None, below=50.0, above=80.0, enabled=True):
    return BackgroundingPolicy(phone or Phone(), lambda: enabled,
                               lambda: below, lambda: above)


# -- the hysteresis band --------------------------------------------------------

def test_engages_below_the_low_threshold():
    p = _policy()
    assert p.on_battery(49.0) is True
    assert p.active is True


def test_does_not_engage_at_the_threshold_itself():
    """Strictly below, so a battery resting exactly on 50 does not flap."""
    p = _policy()
    p.on_battery(50.0)
    assert p.active is False


def test_stays_engaged_through_the_band():
    """The whole point of hysteresis: between the thresholds nothing changes."""
    p = _policy()
    p.on_battery(40.0)
    for level in (55.0, 65.0, 75.0, 79.9):
        p.on_battery(level)
        assert p.active is True, f"disengaged at {level}, inside the band"


def test_disengages_above_the_high_threshold():
    p = _policy()
    p.on_battery(40.0)
    assert p.on_battery(81.0) is True
    assert p.active is False


def test_stays_disengaged_through_the_band_on_the_way_down():
    p = _policy()
    p.on_battery(40.0)
    p.on_battery(85.0)
    for level in (79.0, 65.0, 51.0):
        p.on_battery(level)
        assert p.active is False, f"engaged at {level} without crossing the low threshold"


def test_a_full_cycle():
    p = _policy()
    assert p.active is False
    p.on_battery(30.0); assert p.active is True
    p.on_battery(60.0); assert p.active is True     # recovering, still saving
    p.on_battery(90.0); assert p.active is False    # recovered
    p.on_battery(60.0); assert p.active is False    # dropping, not yet low
    p.on_battery(20.0); assert p.active is True


def test_on_battery_reports_whether_anything_changed():
    """The caller logs on a change, so a policy that reported one every reading
    would fill the log with nothing."""
    p = _policy()
    assert p.on_battery(30.0) is True
    assert p.on_battery(29.0) is False
    assert p.on_battery(31.0) is False


def test_an_unknown_battery_level_changes_nothing():
    """The cable can be out. Guessing would either strand the app backgrounded or
    give up the saving on a phone that needs it."""
    p = _policy()
    p.on_battery(30.0)
    assert p.on_battery(None) is False
    assert p.active is True


# -- the switch -----------------------------------------------------------------

def test_the_switch_off_prevents_engaging():
    p = _policy(enabled=False)
    p.on_battery(10.0)
    assert p.active is False


def test_turning_the_switch_off_releases_the_phone():
    """Otherwise switching it off would strand the app backgrounded, and the
    camera would stay dark until the battery happened to recover."""
    phone = Phone()
    enabled = [True]
    p = BackgroundingPolicy(phone, lambda: enabled[0], lambda: 50.0, lambda: 80.0)
    p.on_battery(10.0)
    p.on_session_close()
    assert phone.calls == ["background"]

    enabled[0] = False
    p.on_battery(10.0)
    assert p.active is False
    assert phone.calls == ["background", "foreground"], "must undo the backgrounding"


# -- driving the phone ----------------------------------------------------------

def test_backgrounds_when_a_session_closes_while_engaged():
    phone = Phone()
    p = _policy(phone)
    p.on_battery(30.0)
    p.on_session_close()
    assert phone.calls == ["background"]


def test_does_nothing_on_session_close_when_not_engaged():
    phone = Phone()
    p = _policy(phone)
    p.on_battery(95.0)
    p.on_session_close()
    assert phone.calls == []


def test_foregrounds_when_a_session_opens_while_engaged():
    phone = Phone()
    p = _policy(phone)
    p.on_battery(30.0)
    p.on_session_close()
    p.on_session_open()
    assert phone.calls == ["background", "foreground"]


def test_does_not_foreground_when_it_never_backgrounded():
    """Calling foreground on an already-foreground app is a wasted usbmux round
    trip on every single capture."""
    phone = Phone()
    p = _policy(phone)
    p.on_battery(30.0)
    p.on_session_open()
    assert phone.calls == []


def test_does_not_background_twice():
    phone = Phone()
    p = _policy(phone)
    p.on_battery(30.0)
    p.on_session_close()
    p.on_session_close()
    assert phone.calls == ["background"]


def test_a_failure_to_background_leaves_the_state_honest():
    """If the cable drops mid-call the app is still in the foreground, and the
    policy must not believe otherwise -- it would then skip the foreground before
    the next capture and the stream would be missing."""
    phone = Phone(fail=RuntimeError("cable gone"))
    p = _policy(phone)
    p.on_battery(30.0)
    p.on_session_close()
    assert p.backgrounded is False


def test_a_failure_to_foreground_is_retried_next_time():
    phone = Phone()
    p = _policy(phone)
    p.on_battery(30.0)
    p.on_session_close()
    phone.fail = RuntimeError("cable gone")
    p.on_session_open()
    assert p.backgrounded is True, "still backgrounded, so it must try again"
    phone.fail = None
    p.on_session_open()
    assert phone.calls == ["background", "foreground", "foreground"]
    assert p.backgrounded is False


def test_no_phone_configured_is_inert():
    p = BackgroundingPolicy(None, lambda: True, lambda: 50.0, lambda: 80.0)
    p.on_battery(10.0)
    p.on_session_close()
    p.on_session_open()
    assert p.active is False


# -- misconfiguration -----------------------------------------------------------

@pytest.mark.parametrize("below,above", [(80.0, 50.0), (50.0, 50.0)])
def test_inverted_thresholds_disable_the_feature(below, above):
    """Home Assistant lets both numbers be set freely, so resume_above can be put
    below background_below. Rather than thrash on every reading, refuse to engage."""
    p = _policy(below=below, above=above)
    p.on_battery(10.0)
    assert p.active is False


# -- wiring into the supervisor -------------------------------------------------
#
# The policy above is pure decision-making. These check it is actually connected:
# a correct policy that nothing calls saves no battery at all.

def _sup(phone, **settings):
    from tests.test_supervisor import FakeArchive, FakeMqtt, Inline

    from ios2ha_camera.ring_buffer import RingBuffer
    from ios2ha_camera.settings import Settings
    from ios2ha_camera.supervisor import Supervisor
    s = Settings()
    for k, v in settings.items():
        setattr(s, k, v)
    return Supervisor(s, RingBuffer(1 << 20), FakeMqtt(), FakeArchive(),
                      phone=phone, device_executor=Inline())


class BatteryPhone(Phone):
    def __init__(self, level):
        super().__init__()
        self._level = level

    def battery(self):
        return {"level": self._level, "temperature": 25.0, "charging": True,
                "current_ma": 300, "charger_watts": 3, "health_pct": 80.0}

    def is_running(self):
        return True


def test_a_battery_reading_drives_the_policy():
    """phone_state() is the only place the battery is read, so if it does not
    feed the policy the feature never engages at all."""
    phone = BatteryPhone(30)
    sup = _sup(phone)
    sup.phone_state()
    assert sup.backgrounding.active is True


def test_a_healthy_battery_leaves_it_disengaged():
    sup = _sup(BatteryPhone(95))
    sup.phone_state()
    assert sup.backgrounding.active is False


def test_the_switch_is_honoured_through_settings():
    sup = _sup(BatteryPhone(10), backgrounding=False)
    sup.phone_state()
    assert sup.backgrounding.active is False


def test_thresholds_are_read_from_settings():
    sup = _sup(BatteryPhone(60), background_below_pct=70.0, resume_above_pct=90.0)
    sup.phone_state()
    assert sup.backgrounding.active is True, "60% is below a 70% threshold"


def test_session_hooks_reach_the_phone():
    phone = BatteryPhone(30)
    sup = _sup(phone)
    sup.phone_state()
    phone.calls.clear()

    sup.on_session_close()
    assert phone.calls == ["background"]
    sup.on_session_open()
    assert phone.calls == ["background", "foreground"]


def test_session_hooks_are_inert_when_disengaged():
    """Every capture calls these. With a healthy battery they must not spend a
    usbmux round trip doing nothing."""
    phone = BatteryPhone(95)
    sup = _sup(phone)
    sup.phone_state()
    phone.calls.clear()

    sup.on_session_close()
    sup.on_session_open()
    assert phone.calls == []


def test_backgrounding_active_is_published():
    phone = BatteryPhone(30)
    sup = _sup(phone)
    sup.phone_state()

    from ios2ha_camera.mqtt_io import discovery_payloads
    from ios2ha_camera.settings import Settings
    binaries = {t.split("/")[3] for t, _ in discovery_payloads(Settings())
                if t.startswith("homeassistant/binary_sensor/")}
    assert "backgrounding_active" in binaries


def test_upstream_calls_the_hooks_around_a_session():
    """The hooks have to fire from the connection lifecycle, and in the right
    order: wake before ingest connects, sleep only once it has closed."""
    from ios2ha_camera.ring_buffer import Frame, RingBuffer
    from ios2ha_camera.settings import Settings
    from ios2ha_camera.upstream import Upstream

    class Ing:
        def start(self): events.append("ingest.start")
        def stop(self, timeout=None):
            events.append("ingest.stop")
            return True

    events = []
    buf = RingBuffer(1 << 20)
    up = Upstream(Ing(), buf, Settings(), probe=lambda: Frame(b"j", 1000.0),
                  on_session_open=lambda: events.append("wake"),
                  on_session_close=lambda: events.append("sleep"))
    h = up.hold("capture")
    up.tick(now=1000.0)
    h.release()
    up.tick(now=1001.0)

    assert events == ["wake", "ingest.start", "ingest.stop", "sleep"]


def test_a_raising_hook_does_not_break_the_connection_lifecycle():
    from ios2ha_camera.ring_buffer import Frame, RingBuffer
    from ios2ha_camera.settings import Settings
    from ios2ha_camera.upstream import Upstream

    class Ing:
        started = False
        def start(self): Ing.started = True
        def stop(self, timeout=None): return True

    buf = RingBuffer(1 << 20)
    up = Upstream(Ing(), buf, Settings(), probe=lambda: Frame(b"j", 1000.0),
                  on_session_open=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with up.hold("capture"):
        up.tick(now=1000.0)
    assert Ing.started is True, "a failed power-saving hook must not stop ingest"


# -- the offline alert ----------------------------------------------------------

def test_a_deliberate_backgrounding_does_not_read_as_offline():
    """Without this the five-minute offline alert fires every night the battery
    runs low, and an alert that cries wolf nightly is one nobody reads."""
    phone = BatteryPhone(30)
    sup = _sup(phone)
    sup.phone_state()
    sup.on_session_close()
    assert sup.backgrounding.backgrounded is True

    from ios2ha_camera.supervisor import Supervisor  # noqa: F401
    sup.upstream = _StubUpstream("backgrounded")
    assert sup.health_state()["camera_online"] is True


def test_a_backgrounding_we_did_not_do_still_reads_as_offline():
    """A phone that wandered into the background on its own will not come back,
    so that must still alert."""
    sup = _sup(BatteryPhone(95))
    sup.phone_state()
    assert sup.backgrounding.backgrounded is False
    sup.upstream = _StubUpstream("backgrounded")
    assert sup.health_state()["camera_online"] is False


def test_a_genuine_outage_still_reads_as_offline_while_engaged():
    """Engaging the policy must not mask a phone that has actually gone."""
    phone = BatteryPhone(30)
    sup = _sup(phone)
    sup.phone_state()
    sup.on_session_close()
    sup.upstream = _StubUpstream("unavailable")
    assert sup.health_state()["camera_online"] is False


class _StubUpstream:
    def __init__(self, state):
        self._state = state

    def state(self, now=None):
        return self._state

    def holders(self, now=None):
        return ()

    def camera_online(self, now=None):
        return False

    def duty_1h(self, now=None):
        return 0.0

    def tick_age(self, now=None):
        return 0.1

    def session_start_ts(self):
        return None
