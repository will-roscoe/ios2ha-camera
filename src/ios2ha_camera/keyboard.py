"""arcturus as a Bluetooth LE keyboard, so the supervisor can press a button
that exists only on the phone's screen.

Guardian Eye starts streaming from an in-app button and nothing else: no URL
scheme, no intent, and the accessibility service's own press is refused for App
Store apps (measured 2026-09-15). iOS Full Keyboard Access, however, turns real
key presses into focus moves and activations, and a Bluetooth keyboard is real
enough.

BlueZ is reached through a filtered D-Bus proxy (`/run/bluez-proxy/bus`), so this
container can talk to org.bluez and nothing else. bluetoothd and its input plugin
are untouched, so the host's own Bluetooth keyboard and mouse keep working.

Nothing here knows any address or device name: the phone is whatever pairs, so it
can be replaced without a config change.
"""

import asyncio
import contextlib
import logging
import os
import threading
import time

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.constants import PropertyAccess
from dbus_fast.service import ServiceInterface, dbus_property, method

from .hid import RELEASE, REPORT_MAP, UnknownKey, report

log = logging.getLogger(__name__)

HID_UUID = "00001812-0000-1000-8000-00805f9b34fb"
BATTERY_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
DEVINFO_UUID = "0000180a-0000-1000-8000-00805f9b34fb"
BLUEZ = "org.bluez"
ADAPTER = "/org/bluez/hci0"
APP_ROOT = "/org/ios2ha/kbd"
ADVERT_PATH = "/org/ios2ha/kbd/advertisement"
AGENT_PATH = "/org/ios2ha/kbd/agent"
# iOS shows the adapter's own name, not this one, so the phone lists the host's
# Bluetooth name. Left as is deliberately: renaming the adapter would rename
# arcturus for every other Bluetooth device.
ADVERT_NAME = "ios2ha-kbd"
KEYBOARD_APPEARANCE = 0x03C1
POLL_S = 10.0
# How long shutdown waits for BlueZ to take the advertisement back. Well inside
# the 3 s __main__ allows the keyboard, because the rest of the stop has to fit
# there too.
UNREGISTER_TIMEOUT_S = 1.5

# Fixed attribute handles, requested from BlueZ rather than left to it.
#
# A real keyboard's handles live in its firmware, so power-cycling it changes
# nothing and the phone's cached HID description still fits. Ours are built at
# registration, and BlueZ hands out fresh ones each time rather than reusing the
# old: measured 0x00b8 -> 0x00cc -> 0x00e0 across three container restarts on
# 2026-09-16. iOS reconnects and re-discovers but never re-reads the report map,
# so it keeps matching our reports against the handles it learned at pairing --
# and every restart silently broke the keyboard until it was paired again.
# Pinning them makes each registration reproduce the same database.
#
# Spaced by two because each characteristic takes a declaration and a value
# handle, and set well clear of the services BlueZ registers for itself.
HID_HANDLE = 0x0200
BATTERY_HANDLE = 0x0220
DEVINFO_HANDLE = 0x0230


class _Reports:
    """Turns key names into notified HID reports, holding each key long enough.

    Nothing is sent unless every name is known: a half-sent sequence leaves the
    phone's focus somewhere unknown, and a later Space could activate anything.
    """

    def __init__(self, notifier, hold_ms=150, gap_s=0.35, sleep=time.sleep):
        self._notifier = notifier
        self._hold_s = hold_ms / 1000.0
        self._gap_s = gap_s
        self._sleep = sleep

    def _plan(self, keys):
        """The reports to send, or None if any name is unknown."""
        try:
            return [report(k) for k in keys]
        except UnknownKey as e:
            log.warning("refusing the whole sequence: unknown key %s", e)
            return None

    def press(self, keys):
        """Blocking form, used by the tests and anything already on the loop's thread."""
        downs = self._plan(keys)
        if downs is None:
            return False
        for i, down in enumerate(downs):
            if not self._notifier.notify(down):
                log.warning("key %r not sent: no host subscribed", keys[i])
                return False
            # Held well past one connection interval: at 40 ms presses vanished,
            # because BlueZ notifies a characteristic's *current* value and the
            # release overwrote the press before it went out.
            self._sleep(self._hold_s)
            self._notifier.notify(RELEASE)
            if i + 1 < len(downs):
                self._sleep(self._gap_s)
        return True

    async def apress(self, keys):
        """The same sequence, awaited on the bus's own loop.

        Notifications must be emitted from the loop thread; this is the form the
        supervisor's worker reaches through `Keyboard.press`.
        """
        downs = self._plan(keys)
        if downs is None:
            return False
        for i, down in enumerate(downs):
            if not self._notifier.notify(down):
                log.warning("key %r not sent: no host subscribed", keys[i])
                return False
            await asyncio.sleep(self._hold_s)
            self._notifier.notify(RELEASE)
            if i + 1 < len(downs):
                await asyncio.sleep(self._gap_s)
        return True


class _Advertiser:
    """Advertise only while no host is attached.

    A host is any connected device that does not itself offer the HID service:
    the host's own keyboard and mouse do, a phone does not. Addresses are never
    matched, so a replacement phone behaves identically.
    """

    def __init__(self, manager):
        self._m = manager
        self._want = False
        self._host = None
        self._subscribed = False

    @property
    def advertising(self):
        return self._m.registered

    def start(self):
        self._want = True
        self._register()

    def stop(self):
        """Stop advertising, returning whatever the unregister is waiting on.

        Shutdown awaits it; the other callers below are reacting to a host
        coming and going, where there is nothing to wait for.
        """
        self._want = False
        if self._m.registered:
            return self._m.unregister()
        return None

    def retry(self):
        """Called on a timer: the adapter refuses to advertise while it still
        holds a peripheral link, so one attempt at startup is not enough."""
        self._register()

    def _register(self):
        if not self._want or self._m.registered or self._subscribed or self._host:
            return
        try:
            self._m.register()
        except Exception as e:
            log.info("advertising not accepted yet (%s); will retry", e)

    def on_subscribed(self, subscribed):
        self._subscribed = subscribed
        if subscribed:
            if self._m.registered:
                self._m.unregister()
        else:
            self._host = None
            self._register()

    def on_device_connected(self, path, uuids):
        if any(str(u).lower() == HID_UUID for u in uuids):
            return      # the host's own keyboard or mouse, not our phone
        self._host = path
        if self._m.registered:
            self._m.unregister()

    def on_device_disconnected(self, path):
        if path == self._host:
            self._host = None
            self._subscribed = False
            self._register()


class _AdManager:
    """`_Advertiser`'s view of BlueZ: register/unregister, and whether it stuck.

    The D-Bus calls are coroutines but `_Advertiser` is synchronous (it is the
    part worth unit-testing), so registration is optimistic and corrected by the
    call's own result; a failure clears the flag and the next retry tries again.
    """

    def __init__(self, loop, register_coro, unregister_coro):
        self._loop = loop
        self._register_coro = register_coro
        self._unregister_coro = unregister_coro
        self.registered = False

    def register(self):
        self.registered = True
        task = self._loop.create_task(self._register_coro())
        task.add_done_callback(self._after_register)

    def _after_register(self, task):
        exc = task.exception()
        if exc is not None:
            self.registered = False
            log.info("advertising rejected: %s", exc)
        else:
            log.info("advertising as a keyboard")

    def unregister(self):
        self.registered = False
        return self._loop.create_task(self._unregister_coro())


class Keyboard:
    """The supervisor's handle on the emulated keyboard.

    Owns an asyncio loop on its own thread; `press` is called from the device
    worker and blocks that caller only. When the proxy socket or BlueZ is
    missing, `state()` is "unavailable" and everything else carries on as before.
    """

    def __init__(self, bus_address=None, hold_ms=150, gap_s=0.35):
        self._address = bus_address or os.environ.get("DBUS_SYSTEM_BUS_ADDRESS")
        self._hold_ms, self._gap_s = hold_ms, gap_s
        self._loop = None
        self._thread = None
        self._bus = None
        self._report = None
        self._reports = None
        self._advertiser = None
        self._connected_paths = {}
        self._ready = threading.Event()
        self._error = None
        self._poll_task = None

    # -- lifecycle ------------------------------------------------------------

    def start(self, timeout=20.0):
        self._thread = threading.Thread(target=self._run, name="keyboard", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=timeout)
        return self.state()

    def stop(self, timeout=5.0):
        """Unregister the advertisement, end the poll, then stop the loop.

        Stopping the loop on its own left the poll task pending, which asyncio
        reported as "Task was destroyed but it is pending!" on every shutdown,
        and left the advertisement registered until the bus connection dropped.
        The thread is a daemon, so a shutdown that outlasts `timeout` costs the
        caller nothing beyond the wait.
        """
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(lambda: loop.create_task(self._shutdown()))
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    async def _shutdown(self):
        if self._advertiser is not None:
            try:
                pending = self._advertiser.stop()
                if pending is not None:
                    await asyncio.wait_for(pending, UNREGISTER_TIMEOUT_S)
            except (TimeoutError, asyncio.CancelledError):
                # BlueZ is slow or gone. The advertisement goes with the bus
                # connection anyway, and the caller's budget is a few seconds.
                log.info("keyboard: gave up unregistering the advertisement")
            except Exception as e:
                log.info("keyboard: unregistering failed: %s: %s", type(e).__name__, e)
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        self._loop.stop()

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._setup())
        except Exception as e:
            self._error = e
            log.warning("keyboard unavailable: %s: %s", type(e).__name__, e)
            self._ready.set()
            return
        self._ready.set()
        self._poll_task = self._loop.create_task(self._poll())
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()

    # -- what the supervisor asks ---------------------------------------------

    def connected(self):
        return bool(self._report is not None and self._report.notifying)

    def state(self):
        if self._error is not None or self._report is None:
            return "unavailable"
        if self.connected():
            return "connected"
        if self._advertiser is not None and self._advertiser.advertising:
            return "advertising"
        return "unavailable"

    def press(self, keys):
        """Send a key sequence. Blocks the calling thread until it is out.

        The work runs on the keyboard's own loop, never on the caller's thread:
        dbus-fast is asyncio, and emitting a signal from a foreign thread does
        not reach the bus. Keys "sent" that way vanish silently, which is what
        happened on 2026-09-16 -- the phone stayed subscribed and nothing moved.
        """
        if self._reports is None or self._loop is None or not self.connected():
            return False
        future = asyncio.run_coroutine_threadsafe(self._reports.apress(keys), self._loop)
        # Long enough for the whole sequence: each key is held then followed by
        # a gap, plus a margin for a slow bus.
        budget = len(keys) * (self._hold_ms / 1000.0 + self._gap_s) + 15.0
        try:
            return future.result(timeout=budget)
        except Exception as e:
            log.warning("key sequence failed: %s: %s", type(e).__name__, e)
            return False

    # -- bus plumbing ---------------------------------------------------------

    async def _setup(self):
        bus = MessageBus(bus_address=self._address) if self._address else \
            MessageBus(bus_type=BusType.SYSTEM)
        self._bus = await bus.connect()

        services, chrcs, descs, self._report = _build_objects(self._on_subscribed)
        exported = [*services, *chrcs, *descs]
        self._bus.export(APP_ROOT, _ObjectManager(exported))
        for obj in exported:
            self._bus.export(obj.path, obj)

        advert = _Advert()
        agent = _Agent()
        self._bus.export(ADVERT_PATH, advert)
        self._bus.export(AGENT_PATH, agent)

        adapter = await self._iface(ADAPTER, "org.bluez.GattManager1")
        await adapter.call_register_application(APP_ROOT, {})
        # BlueZ writes back the handle it really used, so this says whether the
        # requested ones were honoured. They must not move between restarts: iOS
        # never re-reads the report map, so a moved report handle silently breaks
        # the pairing until the phone is paired again.
        log.info("keyboard registered with BlueZ (report handle 0x%04x, asked for 0x%04x)",
                 self._report._handle, HID_HANDLE + 0x08)

        agents = await self._iface("/org/bluez", "org.bluez.AgentManager1")
        await agents.call_register_agent(AGENT_PATH, "NoInputNoOutput")
        await agents.call_request_default_agent(AGENT_PATH)

        ads = await self._iface(ADAPTER, "org.bluez.LEAdvertisingManager1")
        self._advertiser = _Advertiser(_AdManager(
            self._loop,
            lambda: ads.call_register_advertisement(ADVERT_PATH, {}),
            lambda: ads.call_unregister_advertisement(ADVERT_PATH),
        ))
        props = await self._iface(ADAPTER, "org.freedesktop.DBus.Properties")
        await self._set_pairable(props)
        self._reports = _Reports(self._report, self._hold_ms, self._gap_s)
        self._advertiser.start()

    async def _iface(self, path, name):
        introspection = await self._bus.introspect(BLUEZ, path)
        return self._bus.get_proxy_object(BLUEZ, path, introspection).get_interface(name)

    async def _set_pairable(self, props):
        # Pairable with no timeout: the phone may need to pair at any hour, and
        # advertising stops as soon as a host is attached, which is the real
        # limit on who can reach this.
        await props.call_set("org.bluez.Adapter1", "Powered", Variant("b", True))
        await props.call_set("org.bluez.Adapter1", "PairableTimeout", Variant("u", 0))
        await props.call_set("org.bluez.Adapter1", "Pairable", Variant("b", True))

    def _on_subscribed(self, subscribed):
        if self._advertiser is not None:
            self._advertiser.on_subscribed(subscribed)

    async def _poll(self):
        """Watch which devices are connected, and keep advertising alive.

        Polling rather than signals: through the filtering proxy a match rule is
        one more thing that has to be permitted, while GetManagedObjects is a
        plain call the proxy already passes.
        """
        while True:
            await asyncio.sleep(POLL_S)
            try:
                await self._poll_once()
            except Exception as e:
                log.info("keyboard poll failed: %s: %s", type(e).__name__, e)

    async def _poll_once(self):
        om = await self._iface("/", "org.freedesktop.DBus.ObjectManager")
        objects = await om.call_get_managed_objects()
        now = {}
        for path, ifaces in objects.items():
            dev = ifaces.get("org.bluez.Device1")
            if dev is None or not _variant(dev.get("Connected"), False):
                continue
            now[path] = [str(u) for u in _variant(dev.get("UUIDs"), [])]
        for path in set(self._connected_paths) - set(now):
            self._advertiser.on_device_disconnected(path)
        for path, uuids in now.items():
            if path not in self._connected_paths:
                self._advertiser.on_device_connected(path, uuids)
        self._connected_paths = now
        self._advertiser.retry()


def _variant(value, default):
    return default if value is None else value.value


def _build_objects(on_subscribed):
    """The GATT tree: HID, battery and device information.

    Returns (services, characteristics, descriptors, input_report): the first
    three are exported and listed for BlueZ, the last is what `press` notifies.
    """
    hid = _Service(f"{APP_ROOT}/service0", HID_UUID, HID_HANDLE)
    battery = _Service(f"{APP_ROOT}/service1", BATTERY_UUID, BATTERY_HANDLE)
    devinfo = _Service(f"{APP_ROOT}/service2", DEVINFO_UUID, DEVINFO_HANDLE)

    # encrypt-read: iOS only bonds with (and trusts) a HID device whose
    # characteristics require an encrypted link.
    info = _Chrc(f"{hid.path}/char0", "2a4a", ["encrypt-read"], hid.path,
                 bytes([0x11, 0x01, 0x00, 0x02]), handle=HID_HANDLE + 0x02)
    report_map = _Chrc(f"{hid.path}/char1", "2a4b", ["encrypt-read"], hid.path, REPORT_MAP,
                       handle=HID_HANDLE + 0x04)
    control = _Chrc(f"{hid.path}/char2", "2a4c", ["encrypt-write", "write-without-response"],
                    hid.path, bytes([0]), handle=HID_HANDLE + 0x06)
    input_report = _Chrc(f"{hid.path}/char3", "2a4d", ["encrypt-read", "notify"], hid.path,
                         bytes(8), on_subscribed, handle=HID_HANDLE + 0x08)
    protocol = _Chrc(f"{hid.path}/char4", "2a4e", ["encrypt-read", "write-without-response"],
                     hid.path, bytes([0x01]), handle=HID_HANDLE + 0x0C)
    # Report ID 1, type 1 (input): without this the phone cannot match the
    # notifications to the report map. Sits just after the report's own value
    # handle, where a client expects to find it.
    reference = _Desc(f"{input_report.path}/desc0", "2908", ["encrypt-read"],
                      input_report.path, bytes([0x01, 0x01]), handle=HID_HANDLE + 0x0A)

    level = _Chrc(f"{battery.path}/char0", "2a19", ["read", "notify"], battery.path, bytes([100]),
                  handle=BATTERY_HANDLE + 0x02)
    # PnP id: USB vendor source, Linux Foundation, so the phone has something to
    # show rather than an unnamed accessory.
    pnp = _Chrc(f"{devinfo.path}/char0", "2a50", ["read"], devinfo.path,
                bytes([0x02, 0x6B, 0x1D, 0x46, 0x02, 0x01, 0x00]),
                handle=DEVINFO_HANDLE + 0x02)

    return ([hid, battery, devinfo],
            [info, report_map, control, input_report, protocol, level, pnp],
            [reference],
            input_report)


# -- exported D-Bus objects ---------------------------------------------------
#
# dbus-fast reads each method's annotations for its signature, so the "s"/"ay"
# style annotations below are load-bearing, not type hints.



class _Service(ServiceInterface):
    def __init__(self, path, uuid, handle=0):
        super().__init__("org.bluez.GattService1")
        self.path, self._uuid, self._handle = path, uuid, handle

    @dbus_property(access=PropertyAccess.READ, name="UUID")
    def uuid(self) -> "s":  # noqa: F722,F821
        return self._uuid

    @dbus_property(access=PropertyAccess.READ, name="Primary")
    def primary(self) -> "b":  # noqa: F722,F821
        return True

    @dbus_property(access=PropertyAccess.READWRITE, name="Handle")
    def handle(self) -> "q":  # noqa: F722,F821
        return self._handle

    @handle.setter  # BlueZ writes back what it actually allocated
    def handle(self, value: "q"):  # noqa: F722,F821
        self._handle = value

    def managed(self):
        return {"org.bluez.GattService1": {
            "UUID": Variant("s", self._uuid), "Primary": Variant("b", True),
            "Handle": Variant("q", self._handle)}}


class _Chrc(ServiceInterface):
    def __init__(self, path, uuid, flags, service_path, value=b"", on_subscribed=None, handle=0):
        super().__init__("org.bluez.GattCharacteristic1")
        self.path, self._uuid, self._flags = path, uuid, flags
        self._service, self._value = service_path, bytes(value)
        self._on_subscribed = on_subscribed
        self._handle = handle
        self.notifying = False

    @dbus_property(access=PropertyAccess.READWRITE, name="Handle")
    def handle(self) -> "q":  # noqa: F722,F821
        return self._handle

    @handle.setter  # BlueZ writes back what it actually allocated
    def handle(self, value: "q"):  # noqa: F722,F821
        self._handle = value

    @dbus_property(access=PropertyAccess.READ, name="UUID")
    def uuid(self) -> "s":  # noqa: F722,F821
        return self._uuid

    @dbus_property(access=PropertyAccess.READ, name="Service")
    def service(self) -> "o":  # noqa: F722,F821
        return self._service

    @dbus_property(access=PropertyAccess.READ, name="Flags")
    def flags(self) -> "as":  # noqa: F722,F821
        return self._flags

    @dbus_property(access=PropertyAccess.READ, name="Value")
    def value(self) -> "ay":  # noqa: F722,F821
        return self._value

    @method(name="ReadValue")
    def read_value(self, options: "a{sv}") -> "ay":  # noqa: F722,F821
        return self._value

    @method(name="WriteValue")
    def write_value(self, value: "ay", options: "a{sv}"):  # noqa: F722,F821
        self._value = bytes(value)

    @method(name="StartNotify")
    def start_notify(self):
        self.notifying = True
        log.info("a host subscribed to %s", self._uuid)
        if self._on_subscribed:
            self._on_subscribed(True)

    @method(name="StopNotify")
    def stop_notify(self):
        self.notifying = False
        log.info("a host unsubscribed from %s", self._uuid)
        if self._on_subscribed:
            self._on_subscribed(False)

    def notify(self, data):
        """Push a value to the subscribed host. False means nobody is listening."""
        self._value = bytes(data)
        if not self.notifying:
            return False
        self.emit_properties_changed({"Value": self._value})
        return True

    def managed(self):
        return {"org.bluez.GattCharacteristic1": {
            "UUID": Variant("s", self._uuid),
            "Service": Variant("o", self._service),
            "Flags": Variant("as", self._flags),
            "Handle": Variant("q", self._handle)}}


class _Desc(ServiceInterface):
    def __init__(self, path, uuid, flags, chrc_path, value=b"", handle=0):
        super().__init__("org.bluez.GattDescriptor1")
        self.path, self._uuid, self._flags = path, uuid, flags
        self._chrc, self._value = chrc_path, bytes(value)
        self._handle = handle

    @dbus_property(access=PropertyAccess.READWRITE, name="Handle")
    def handle(self) -> "q":  # noqa: F722,F821
        return self._handle

    @handle.setter  # BlueZ writes back what it actually allocated
    def handle(self, value: "q"):  # noqa: F722,F821
        self._handle = value

    @dbus_property(access=PropertyAccess.READ, name="UUID")
    def uuid(self) -> "s":  # noqa: F722,F821
        return self._uuid

    @dbus_property(access=PropertyAccess.READ, name="Characteristic")
    def characteristic(self) -> "o":  # noqa: F722,F821
        return self._chrc

    @dbus_property(access=PropertyAccess.READ, name="Flags")
    def flags(self) -> "as":  # noqa: F722,F821
        return self._flags

    @method(name="ReadValue")
    def read_value(self, options: "a{sv}") -> "ay":  # noqa: F722,F821
        return self._value

    def managed(self):
        return {"org.bluez.GattDescriptor1": {
            "UUID": Variant("s", self._uuid),
            "Characteristic": Variant("o", self._chrc),
            "Flags": Variant("as", self._flags),
            "Handle": Variant("q", self._handle)}}


class _ObjectManager(ServiceInterface):
    """BlueZ reads the whole GATT tree from here when the app registers."""

    def __init__(self, objects):
        super().__init__("org.freedesktop.DBus.ObjectManager")
        self._objects = objects

    @method(name="GetManagedObjects")
    def get_managed_objects(self) -> "a{oa{sa{sv}}}":  # noqa: F722,F821
        return {o.path: o.managed() for o in self._objects}


class _Advert(ServiceInterface):
    def __init__(self):
        super().__init__("org.bluez.LEAdvertisement1")

    @dbus_property(access=PropertyAccess.READ, name="Type")
    def type_(self) -> "s":  # noqa: F722,F821
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ, name="ServiceUUIDs")
    def service_uuids(self) -> "as":  # noqa: F722,F821
        return ["1812", "180F"]

    @dbus_property(access=PropertyAccess.READ, name="Appearance")
    def appearance(self) -> "q":  # noqa: F722,F821
        return KEYBOARD_APPEARANCE

    @dbus_property(access=PropertyAccess.READ, name="LocalName")
    def local_name(self) -> "s":  # noqa: F722,F821
        return ADVERT_NAME

    @dbus_property(access=PropertyAccess.READ, name="Discoverable")
    def discoverable(self) -> "b":  # noqa: F722,F821
        return True

    @method(name="Release")
    def release(self):
        log.info("advertisement released by BlueZ")


class _Agent(ServiceInterface):
    """Accepts pairing without a PIN, which is how a keyboard pairs.

    Deliberately not restricted to one device: an address allow-list rejected
    the right phone, because iOS pairs from a random address and only reveals
    its real one while bonding. Advertising stops as soon as a host attaches,
    which is what bounds who can pair.
    """

    def __init__(self):
        super().__init__("org.bluez.Agent1")

    @method(name="Release")
    def release(self):
        pass

    @method(name="AuthorizeService")
    def authorize_service(self, device: "o", uuid: "s"):  # noqa: F722,F821
        log.info("service authorised for %s", device)

    @method(name="RequestConfirmation")
    def request_confirmation(self, device: "o", passkey: "u"):  # noqa: F722,F821
        log.info("pairing confirmed for %s", device)

    @method(name="RequestAuthorization")
    def request_authorization(self, device: "o"):  # noqa: F722,F821
        log.info("pairing authorised for %s", device)

    @method(name="Cancel")
    def cancel(self):
        log.info("pairing cancelled")
