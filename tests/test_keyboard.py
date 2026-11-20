"""The two decisions inside the emulated keyboard that are worth testing.

`_Reports` turns key names into notified reports with the right timing, and
`_Advertiser` decides when to advertise, and stopping has to leave nothing
behind. The dbus-fast glue around them has no branches of its own and is
covered by the on-device verification instead.
"""

import asyncio
import time

from ios2ha_camera.hid import RELEASE, report
from ios2ha_camera.keyboard import HID_UUID, Keyboard, _Advertiser, _Reports


class FakeNotifier:
    """Stands in for the input-report characteristic, recording what it sent."""

    def __init__(self, subscribed=True):
        self.subscribed = subscribed
        self.sent = []

    def notify(self, value):
        if not self.subscribed:
            return False
        self.sent.append((time.monotonic(), bytes(value)))
        return True


def test_a_key_is_held_then_released():
    n = FakeNotifier()
    r = _Reports(n, hold_ms=20, gap_s=0.0)
    assert r.press(["tab"]) is True
    assert [v for _, v in n.sent] == [report("tab"), RELEASE]


def test_the_release_comes_after_the_hold_not_immediately():
    """At 40 ms presses vanished on the real phone: BlueZ notifies a
    characteristic's current value, so a release landing before the press went
    out overwrites it and iOS never sees the key go down."""
    n = FakeNotifier()
    r = _Reports(n, hold_ms=60, gap_s=0.0)
    r.press(["tab"])
    (t_down, _), (t_up, _) = n.sent
    assert t_up - t_down >= 0.055


def test_a_sequence_sends_every_key_in_order_with_a_gap():
    n = FakeNotifier()
    r = _Reports(n, hold_ms=10, gap_s=0.05)
    assert r.press(["tab", "down", "space"]) is True
    assert [v for _, v in n.sent] == [report("tab"), RELEASE,
                                      report("down"), RELEASE,
                                      report("space"), RELEASE]
    downs = [t for t, v in n.sent if v != RELEASE]
    assert downs[1] - downs[0] >= 0.05


def test_nothing_is_sent_when_no_host_is_subscribed():
    """Reporting success for keys nobody received would let the caller believe
    it had pressed Start streaming."""
    n = FakeNotifier(subscribed=False)
    r = _Reports(n, hold_ms=10, gap_s=0.0)
    assert r.press(["tab"]) is False
    assert n.sent == []


def test_an_unknown_key_sends_nothing_at_all():
    """The whole sequence is refused rather than half-sent: a half-sequence
    leaves the focus somewhere unknown, and a later Space could hit anything."""
    n = FakeNotifier()
    r = _Reports(n, hold_ms=10, gap_s=0.0)
    assert r.press(["tab", "nonsense", "space"]) is False
    assert n.sent == []


def test_the_awaited_form_sends_the_same_reports():
    """`press` from the supervisor's worker goes through this, because dbus-fast
    is asyncio and a signal emitted from a foreign thread never reaches the bus:
    on 2026-09-16 every key was "sent" and the phone never moved."""
    import asyncio

    n = FakeNotifier()
    r = _Reports(n, hold_ms=1, gap_s=0.0)
    assert asyncio.run(r.apress(["tab", "space"])) is True
    assert [v for _, v in n.sent] == [report("tab"), RELEASE, report("space"), RELEASE]


def test_the_awaited_form_refuses_an_unknown_key_too():
    import asyncio

    n = FakeNotifier()
    r = _Reports(n, hold_ms=1, gap_s=0.0)
    assert asyncio.run(r.apress(["tab", "nonsense"])) is False
    assert n.sent == []


class FakeAdManager:
    def __init__(self):
        self.registered = False
        self.calls = []
        self.fail_next = False

    def register(self):
        self.calls.append("register")
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("Invalid Parameters")
        self.registered = True

    def unregister(self):
        self.calls.append("unregister")
        self.registered = False


def test_it_advertises_until_a_host_subscribes_then_stops():
    m = FakeAdManager()
    a = _Advertiser(m)
    a.start()
    assert m.registered is True
    a.on_subscribed(True)
    assert m.registered is False


def test_it_advertises_again_when_the_host_goes_away():
    m = FakeAdManager()
    a = _Advertiser(m)
    a.start()
    a.on_subscribed(True)
    a.on_subscribed(False)
    assert m.registered is True


def test_a_failed_advertisement_is_retried():
    """The adapter refuses to advertise while it still holds a peripheral link
    ("Invalid Parameters"), so one attempt at startup is not enough."""
    m = FakeAdManager()
    m.fail_next = True
    a = _Advertiser(m)
    a.start()
    assert m.registered is False
    a.retry()
    assert m.registered is True


def test_host_own_hid_devices_never_count_as_the_host():
    """The host's own keyboard and mouse connect as HID peripherals; a phone
    does not offer the HID service. Never match on address: the phone must be
    replaceable without a config change."""
    m = FakeAdManager()
    a = _Advertiser(m)
    a.start()
    a.on_device_connected("/org/bluez/hci0/dev_AA", uuids=[HID_UUID])
    assert m.registered is True
    a.on_device_connected("/org/bluez/hci0/dev_BB",
                          uuids=["0000180a-0000-1000-8000-00805f9b34fb"])
    assert m.registered is False


def test_a_host_disconnecting_brings_advertising_back():
    m = FakeAdManager()
    a = _Advertiser(m)
    a.start()
    a.on_device_connected("/org/bluez/hci0/dev_BB", uuids=[])
    assert m.registered is False
    a.on_device_disconnected("/org/bluez/hci0/dev_BB")
    assert m.registered is True


def test_retry_does_nothing_while_a_host_is_attached():
    """Retrying every 10 s must not re-advertise behind a connected phone."""
    m = FakeAdManager()
    a = _Advertiser(m)
    a.start()
    a.on_subscribed(True)
    m.calls.clear()
    a.retry()
    assert m.calls == []


# -- shutdown ---------------------------------------------------------------
#
# The keyboard owns a loop on its own thread. Stopping it used to stop that loop
# outright, which left the poll task pending -- asyncio then logged "Task was
# destroyed but it is pending!" on every container stop -- and left the
# advertisement registered with BlueZ, although __main__ claimed otherwise.


class _SlowAdvertiser:
    """Stands in for the real one: records the stop, and takes a moment over it."""

    def __init__(self, delay=0.0):
        self.stopped = False
        self._delay = delay

    def stop(self):
        self.stopped = True

        async def unregister():
            await asyncio.sleep(self._delay)

        return asyncio.get_running_loop().create_task(unregister())


def _started_keyboard(advertiser):
    kb = Keyboard()

    async def fake_setup():
        kb._advertiser = advertiser

    kb._setup = fake_setup
    assert kb.start(timeout=5.0) is not None
    return kb


def test_stopping_cancels_the_poll_task_rather_than_abandoning_it():
    kb = _started_keyboard(_SlowAdvertiser())
    poll = kb._poll_task
    kb.stop(timeout=5.0)
    assert poll.cancelled() or poll.done()
    assert not kb._thread.is_alive()


def test_stopping_unregisters_the_advertisement():
    adv = _SlowAdvertiser()
    kb = _started_keyboard(adv)
    kb.stop(timeout=5.0)
    assert adv.stopped is True


def test_an_unregister_that_hangs_does_not_hold_the_shutdown_open():
    """BlueZ can be slow or gone; the caller's budget is a few seconds, and an
    advertisement dies with the bus connection anyway."""
    kb = _started_keyboard(_SlowAdvertiser(delay=30.0))
    started = time.monotonic()
    kb.stop(timeout=10.0)
    assert time.monotonic() - started < 5.0
    assert not kb._thread.is_alive()
