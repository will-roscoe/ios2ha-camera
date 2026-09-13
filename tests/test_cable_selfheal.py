"""The cable self-heal: restarting the process when usbmuxd's socket is orphaned.

The container bind-mounts usbmuxd's socket as a file. usbmuxd exits whenever no
device is attached and creates a new socket on restart, so the container is left
holding a deleted inode and every cable operation fails until it is recreated.

A service that restarts itself is dangerous, so most of what is worth testing here
is the cases where it must NOT fire: an unplugged phone, a failure that has not
lasted, a process that has only just started, and one that restarted recently.
"""


import pytest
from tests.test_supervisor import FakeArchive, FakeMqtt, FakePhone, Inline

from ios2ha_camera.phone import PhoneUnavailable
from ios2ha_camera.ring_buffer import RingBuffer
from ios2ha_camera.settings import Settings
from ios2ha_camera.supervisor import (
    CABLE_FAILING_FOR_S,
    CABLE_MIN_UPTIME_S,
    CABLE_RESTART_COOLDOWN_S,
    Supervisor,
    is_usbmuxd_down,
)


class Clock:
    """A monotonic clock the test drives by hand."""

    def __init__(self, t=100_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class Restarter:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1


def _reraise_as_phone_unavailable(e):
    """Build exactly what PhoneManager._call raises: a PhoneUnavailable naming the
    original class, with __cause__ set. Tests must go through this rather than
    constructing PhoneUnavailable directly, or they would not exercise the
    unwrapping the detector actually has to do."""
    try:
        raise PhoneUnavailable(f"{type(e).__name__}: {e}") from e
    except PhoneUnavailable as pu:
        return pu


class ConnectionFailedToUsbmuxdError(Exception):
    """Stand-in with the same class name pymobiledevice3 uses."""


class NoDeviceConnectedError(Exception):
    """Stand-in: usbmuxd is answering fine, the phone is simply unplugged."""


def _sup(phone, restarter, clock, **kw):
    return Supervisor(
        Settings(), RingBuffer(1_000_000), FakeMqtt(), FakeArchive(),
        phone=phone, device_executor=Inline(),
        request_restart=restarter, clock=clock, **kw,
    )


def _fail_for(sup, clock, seconds, step=60.0):
    """Keep the cable failing for `seconds`, polling as run_health would."""
    end = clock.t + seconds
    while clock.t < end:
        sup.phone_state()
        clock.advance(step)
    sup.phone_state()


# -- the detector ---------------------------------------------------------------

def test_usbmuxd_failure_is_recognised_through_the_wrapper():
    assert is_usbmuxd_down(_reraise_as_phone_unavailable(ConnectionFailedToUsbmuxdError()))


def test_unplugged_phone_is_not_a_usbmuxd_failure():
    """The distinction the whole feature rests on. NoDeviceConnectedError means
    usbmuxd is answering and there is simply no phone; restarting would be wrong,
    and would happen every time the cable is pulled."""
    assert not is_usbmuxd_down(_reraise_as_phone_unavailable(NoDeviceConnectedError()))


def test_unrelated_failure_is_not_a_usbmuxd_failure():
    assert not is_usbmuxd_down(_reraise_as_phone_unavailable(TimeoutError("timed out")))


def test_detector_survives_a_cause_cycle():
    a = PhoneUnavailable("a")
    b = PhoneUnavailable("b")
    a.__cause__ = b
    b.__cause__ = a
    assert is_usbmuxd_down(a) is False


# -- when it must fire ----------------------------------------------------------

def test_restarts_after_a_sustained_usbmuxd_failure():
    clock = Clock()
    restarter = Restarter()
    phone = FakePhone(error=_reraise_as_phone_unavailable(ConnectionFailedToUsbmuxdError()))
    sup = _sup(phone, restarter, clock)
    clock.advance(CABLE_MIN_UPTIME_S + 1)

    _fail_for(sup, clock, CABLE_FAILING_FOR_S + 60)

    assert restarter.calls == 1


def test_restarts_only_once_per_cooldown():
    """After asking to exit, the process may take seconds to go. It must not ask
    again on every health tick in the meantime."""
    clock = Clock()
    restarter = Restarter()
    phone = FakePhone(error=_reraise_as_phone_unavailable(ConnectionFailedToUsbmuxdError()))
    sup = _sup(phone, restarter, clock)
    clock.advance(CABLE_MIN_UPTIME_S + 1)

    _fail_for(sup, clock, CABLE_FAILING_FOR_S + 60)
    assert restarter.calls == 1

    _fail_for(sup, clock, CABLE_RESTART_COOLDOWN_S - 600)
    assert restarter.calls == 1, "restarted again inside the cooldown"

    _fail_for(sup, clock, 1200)
    assert restarter.calls == 2


# -- when it must not -----------------------------------------------------------

def test_does_not_restart_for_an_unplugged_phone():
    clock = Clock()
    restarter = Restarter()
    phone = FakePhone(error=_reraise_as_phone_unavailable(NoDeviceConnectedError()))
    sup = _sup(phone, restarter, clock)
    clock.advance(CABLE_MIN_UPTIME_S + 1)

    _fail_for(sup, clock, CABLE_FAILING_FOR_S * 4)

    assert restarter.calls == 0


def test_does_not_restart_early_in_the_process_lifetime():
    """A cable already broken at boot would otherwise restart the container
    forever, since every new process finds the same dead socket."""
    clock = Clock()
    restarter = Restarter()
    phone = FakePhone(error=_reraise_as_phone_unavailable(ConnectionFailedToUsbmuxdError()))
    sup = _sup(phone, restarter, clock)

    _fail_for(sup, clock, CABLE_MIN_UPTIME_S - 120)

    assert restarter.calls == 0


def test_does_not_restart_for_a_brief_failure():
    clock = Clock()
    restarter = Restarter()
    phone = FakePhone(error=_reraise_as_phone_unavailable(ConnectionFailedToUsbmuxdError()))
    sup = _sup(phone, restarter, clock)
    clock.advance(CABLE_MIN_UPTIME_S + 1)

    _fail_for(sup, clock, CABLE_FAILING_FOR_S - 120)

    assert restarter.calls == 0


def test_a_success_clears_the_failure_run():
    """A failure that recovers must not count towards the next one, or two
    unrelated blips an hour apart would add up to a restart."""
    clock = Clock()
    restarter = Restarter()
    phone = FakePhone(error=_reraise_as_phone_unavailable(ConnectionFailedToUsbmuxdError()))
    sup = _sup(phone, restarter, clock)
    clock.advance(CABLE_MIN_UPTIME_S + 1)

    _fail_for(sup, clock, CABLE_FAILING_FOR_S - 60)
    phone._error = None
    sup.phone_state()
    phone._error = _reraise_as_phone_unavailable(ConnectionFailedToUsbmuxdError())
    _fail_for(sup, clock, CABLE_FAILING_FOR_S - 60)

    assert restarter.calls == 0


def test_no_phone_configured_never_restarts():
    clock = Clock()
    restarter = Restarter()
    sup = _sup(None, restarter, clock)
    clock.advance(CABLE_MIN_UPTIME_S + 1)

    _fail_for(sup, clock, CABLE_FAILING_FOR_S * 3)

    assert restarter.calls == 0


# -- the reported state ---------------------------------------------------------

def test_cable_failure_is_visible_in_health_state():
    """The restart is a last resort; Home Assistant should be able to alert on
    the condition well before it happens."""
    clock = Clock()
    phone = FakePhone(error=_reraise_as_phone_unavailable(ConnectionFailedToUsbmuxdError()))
    sup = _sup(phone, Restarter(), clock)
    clock.advance(CABLE_MIN_UPTIME_S + 1)

    assert sup.health_state().get("cable_state") == "ok"
    _fail_for(sup, clock, 120)
    assert sup.health_state().get("cable_state") == "usbmuxd_down"


def test_unplugged_phone_reports_as_such_not_as_a_fault():
    clock = Clock()
    phone = FakePhone(error=_reraise_as_phone_unavailable(NoDeviceConnectedError()))
    sup = _sup(phone, Restarter(), clock)
    clock.advance(CABLE_MIN_UPTIME_S + 1)

    _fail_for(sup, clock, 120)

    assert sup.health_state().get("cable_state") == "no_device"


def test_recovery_returns_the_state_to_ok():
    clock = Clock()
    phone = FakePhone(error=_reraise_as_phone_unavailable(ConnectionFailedToUsbmuxdError()))
    sup = _sup(phone, Restarter(), clock)
    clock.advance(CABLE_MIN_UPTIME_S + 1)
    _fail_for(sup, clock, 120)

    phone._error = None
    sup.phone_state()

    assert sup.health_state().get("cable_state") == "ok"


@pytest.mark.parametrize("value", ["ok", "no_device", "usbmuxd_down"])
def test_cable_state_values_are_all_declared_to_home_assistant(value):
    """An enum sensor rejects a state that is not in its options list, so the
    discovery message and the code must not drift apart."""
    from ios2ha_camera.mqtt_io import discovery_payloads

    for topic, cfg in discovery_payloads(Settings()):
        if topic.endswith("/cable_state/config"):
            assert value in cfg["options"]
            return
    pytest.fail("no cable_state entity is published")
