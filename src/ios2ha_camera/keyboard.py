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

    def press(self, keys):
        try:
            downs = [report(k) for k in keys]
        except UnknownKey as e:
            log.warning("refusing the whole sequence: unknown key %s", e)
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
        self._want = False
        if self._m.registered:
            self._m.unregister()

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
        self._loop.create_task(self._unregister_coro())


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

    # -- lifecycle ------------------------------------------------------------

    def start(self, timeout=20.0):
        self._thread = threading.Thread(target=self._run, name="keyboard", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=timeout)
        return self.state()

    def stop(self, timeout=5.0):
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=timeout)

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
        self._loop.create_task(self._poll())
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
        """Send a key sequence. Blocks the calling thread until it is out."""
        if self._reports is None or not self.connected():
            return False
        return self._reports.press(keys)

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
        log.info("keyboard registered with BlueZ")

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
    hid = _Service(f"{APP_ROOT}/service0", HID_UUID)
    battery = _Service(f"{APP_ROOT}/service1", BATTERY_UUID)
    devinfo = _Service(f"{APP_ROOT}/service2", DEVINFO_UUID)

    # encrypt-read: iOS only bonds with (and trusts) a HID device whose
    # characteristics require an encrypted link.
    info = _Chrc(f"{hid.path}/char0", "2a4a", ["encrypt-read"], hid.path,
                 bytes([0x11, 0x01, 0x00, 0x02]))
    report_map = _Chrc(f"{hid.path}/char1", "2a4b", ["encrypt-read"], hid.path, REPORT_MAP)
    control = _Chrc(f"{hid.path}/char2", "2a4c", ["encrypt-write", "write-without-response"],
                    hid.path, bytes([0]))
    input_report = _Chrc(f"{hid.path}/char3", "2a4d", ["encrypt-read", "notify"], hid.path,
                         bytes(8), on_subscribed)
    protocol = _Chrc(f"{hid.path}/char4", "2a4e", ["encrypt-read", "write-without-response"],
                     hid.path, bytes([0x01]))
    # Report ID 1, type 1 (input): without this the phone cannot match the
    # notifications to the report map.
    reference = _Desc(f"{input_report.path}/desc0", "2908", ["encrypt-read"],
                      input_report.path, bytes([0x01, 0x01]))

    level = _Chrc(f"{battery.path}/char0", "2a19", ["read", "notify"], battery.path, bytes([100]))
    # PnP id: USB vendor source, Linux Foundation, so the phone has something to
    # show rather than an unnamed accessory.
    pnp = _Chrc(f"{devinfo.path}/char0", "2a50", ["read"], devinfo.path,
                bytes([0x02, 0x6B, 0x1D, 0x46, 0x02, 0x01, 0x00]))

    return ([hid, battery, devinfo],
            [info, report_map, control, input_report, protocol, level, pnp],
            [reference],
            input_report)


# -- exported D-Bus objects ---------------------------------------------------
#
# dbus-fast reads each method's annotations for its signature, so the "s"/"ay"
# style annotations below are load-bearing, not type hints.



class _Service(ServiceInterface):
    def __init__(self, path, uuid):
        super().__init__("org.bluez.GattService1")
        self.path, self._uuid = path, uuid

    @dbus_property(access=PropertyAccess.READ, name="UUID")
    def uuid(self) -> "s":  # noqa: F722,F821
        return self._uuid

    @dbus_property(access=PropertyAccess.READ, name="Primary")
    def primary(self) -> "b":  # noqa: F722,F821
        return True

    def managed(self):
        return {"org.bluez.GattService1": {
            "UUID": Variant("s", self._uuid), "Primary": Variant("b", True)}}


class _Chrc(ServiceInterface):
    def __init__(self, path, uuid, flags, service_path, value=b"", on_subscribed=None):
        super().__init__("org.bluez.GattCharacteristic1")
        self.path, self._uuid, self._flags = path, uuid, flags
        self._service, self._value = service_path, bytes(value)
        self._on_subscribed = on_subscribed
        self.notifying = False

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
            "Flags": Variant("as", self._flags)}}


class _Desc(ServiceInterface):
    def __init__(self, path, uuid, flags, chrc_path, value=b""):
        super().__init__("org.bluez.GattDescriptor1")
        self.path, self._uuid, self._flags = path, uuid, flags
        self._chrc, self._value = chrc_path, bytes(value)

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
            "Flags": Variant("as", self._flags)}}


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
