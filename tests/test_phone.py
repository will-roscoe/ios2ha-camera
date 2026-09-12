import asyncio

from ios2ha_camera import phone
from ios2ha_camera.phone import PhoneManager, PhoneManagerMisuseError, PhoneUnavailable
import pytest

def test_battery_returns_dict_shape(monkeypatch):
    pm = PhoneManager("com.example")
    monkeypatch.setattr(pm, "_diagnostics_battery", lambda: {"CurrentCapacity": 88, "Temperature": 3200, "IsCharging": True})
    b = pm.battery()
    assert b["level"] == 88 and abs(b["temperature"] - 32.0) < 0.1 and b["charging"] is True

def test_battery_raises_when_no_device(monkeypatch):
    pm = PhoneManager("com.example")
    def boom(): raise RuntimeError("no device")
    monkeypatch.setattr(pm, "_diagnostics_battery", boom)
    with pytest.raises(PhoneUnavailable):
        pm.battery()

def test_battery_missing_fields_default_safely(monkeypatch):
    """An IORegistry snapshot missing keys must not raise -- it should degrade to a
    reportable falsy shape rather than crash the health tick."""
    pm = PhoneManager("com.example")
    monkeypatch.setattr(pm, "_diagnostics_battery", lambda: {})
    b = pm.battery()
    assert b["level"] is None and b["temperature"] == 0.0 and b["charging"] is False

def test_is_running_true_when_bundle_process_present(monkeypatch):
    pm = PhoneManager("com.example.guardianeye")
    monkeypatch.setattr(pm, "_mount_ddi", lambda: None)
    monkeypatch.setattr(pm, "_proclist", lambda: [{"name": "GuardianEye"}, {"name": "SpringBoard"}])
    assert pm.is_running() is True

def test_is_running_false_when_absent(monkeypatch):
    pm = PhoneManager("com.example.guardianeye")
    monkeypatch.setattr(pm, "_mount_ddi", lambda: None)
    monkeypatch.setattr(pm, "_proclist", lambda: [{"name": "SpringBoard"}])
    assert pm.is_running() is False

def test_is_running_raises_when_no_device(monkeypatch):
    pm = PhoneManager("com.example")
    monkeypatch.setattr(pm, "_mount_ddi", lambda: None)
    def boom(): raise RuntimeError("no device")
    monkeypatch.setattr(pm, "_proclist", boom)
    with pytest.raises(PhoneUnavailable):
        pm.is_running()

def test_launch_true_when_pid_returned(monkeypatch):
    pm = PhoneManager("com.example")
    monkeypatch.setattr(pm, "_mount_ddi", lambda: None)
    monkeypatch.setattr(pm, "_launch", lambda: 4242)
    assert pm.launch() is True

def test_launch_raises_when_no_device(monkeypatch):
    pm = PhoneManager("com.example")
    monkeypatch.setattr(pm, "_mount_ddi", lambda: None)
    def boom(): raise RuntimeError("no device")
    monkeypatch.setattr(pm, "_launch", boom)
    with pytest.raises(PhoneUnavailable):
        pm.launch()

def test_screenshot_returns_bytes(monkeypatch):
    pm = PhoneManager("com.example")
    monkeypatch.setattr(pm, "_mount_ddi", lambda: None)
    monkeypatch.setattr(pm, "_take_screenshot", lambda: b"\x89PNG...")
    assert pm.screenshot() == b"\x89PNG..."

def test_screenshot_raises_when_take_screenshot_fails(monkeypatch):
    """Even with the DDI mounted, a failing screenshot call must surface as
    PhoneUnavailable, not crash."""
    pm = PhoneManager("com.example")
    monkeypatch.setattr(pm, "_mount_ddi", lambda: None)
    def boom(): raise RuntimeError("screenshotr didn't return ready state")
    monkeypatch.setattr(pm, "_take_screenshot", boom)
    with pytest.raises(PhoneUnavailable):
        pm.screenshot()

def test_is_running_triggers_mount(monkeypatch):
    pm = PhoneManager("com.example.guardianeye")
    mounted = []
    monkeypatch.setattr(pm, "_mount_ddi", lambda: mounted.append(1))
    monkeypatch.setattr(pm, "_proclist", lambda: [{"name": "GuardianEye"}])
    assert pm.is_running() is True
    assert mounted == [1]

def test_launch_triggers_mount(monkeypatch):
    pm = PhoneManager("com.example")
    mounted = []
    monkeypatch.setattr(pm, "_mount_ddi", lambda: mounted.append(1))
    monkeypatch.setattr(pm, "_launch", lambda: 4242)
    assert pm.launch() is True
    assert mounted == [1]

def test_screenshot_triggers_mount(monkeypatch):
    pm = PhoneManager("com.example")
    mounted = []
    monkeypatch.setattr(pm, "_mount_ddi", lambda: mounted.append(1))
    monkeypatch.setattr(pm, "_take_screenshot", lambda: b"\x89PNG...")
    assert pm.screenshot() == b"\x89PNG..."
    assert mounted == [1]

def test_battery_does_not_mount(monkeypatch):
    """DiagnosticsService needs no DDI -- battery() must stay available (and cheap) on a
    freshly rebooted, unmounted phone. Mounting here would be a regression."""
    pm = PhoneManager("com.example")
    def must_not_be_called():
        raise AssertionError("battery() must not mount the DDI")
    monkeypatch.setattr(pm, "_mount_ddi", must_not_be_called)
    monkeypatch.setattr(pm, "_diagnostics_battery", lambda: {"CurrentCapacity": 50, "Temperature": 3000, "IsCharging": False})
    b = pm.battery()
    assert b["level"] == 50

def _async_return(value):
    async def f():
        return value
    return f

class _FakeLockdown:
    def __init__(self):
        self.closed = False
    async def close(self):
        self.closed = True

def test_mount_ddi_treats_already_mounted_as_success(monkeypatch):
    """A phone that has not rebooted since the last mount answers AlreadyMountedError --
    that is the normal case, not a failure, and must not block is_running/launch/screenshot."""
    from pymobiledevice3.services.mobile_image_mounter import AlreadyMountedError
    pm = PhoneManager("com.example")
    fake = _FakeLockdown()
    monkeypatch.setattr(pm, "_lockdown", _async_return(fake))

    async def already_mounted(lockdown):
        raise AlreadyMountedError()
    monkeypatch.setattr(pm, "_auto_mount", already_mounted)

    pm._mount_ddi()  # must not raise
    assert pm._ddi_mounted is True
    assert fake.closed is True

def test_mount_ddi_failure_surfaces_as_phone_unavailable_with_cause(monkeypatch):
    pm = PhoneManager("com.example")
    monkeypatch.setattr(pm, "_lockdown", _async_return(_FakeLockdown()))

    async def refused(lockdown):
        raise ConnectionRefusedError("mounter service refused")
    monkeypatch.setattr(pm, "_auto_mount", refused)

    with pytest.raises(PhoneUnavailable) as excinfo:
        pm._call(pm._mount_ddi)
    assert "ConnectionRefusedError" in str(excinfo.value)
    assert pm._ddi_mounted is False  # a failure must not be remembered -- the next call retries

def test_mount_ddi_skipped_once_already_mounted(monkeypatch):
    """Lazy: do not re-mount on every call once the first attempt has succeeded."""
    pm = PhoneManager("com.example")
    monkeypatch.setattr(pm, "_lockdown", _async_return(_FakeLockdown()))
    calls = []

    async def record(lockdown):
        calls.append(1)
    monkeypatch.setattr(pm, "_auto_mount", record)

    pm._mount_ddi()
    pm._mount_ddi()
    assert calls == [1]

def test_battery_from_running_event_loop_raises_misuse_not_unavailable(monkeypatch):
    """Calling a blocking PhoneManager method from a coroutine already running on an
    event loop (the natural way to write a health tick in an aiohttp-based supervisor)
    must fail loudly as a caller-side wiring mistake, not look like an unreachable phone."""
    pm = PhoneManager("com.example")
    monkeypatch.setattr(pm, "_diagnostics_battery", lambda: {"CurrentCapacity": 88, "Temperature": 3200, "IsCharging": True})

    async def call_from_loop():
        return pm.battery()

    with pytest.raises(PhoneManagerMisuseError) as exc_info:
        asyncio.run(call_from_loop())
    assert not isinstance(exc_info.value, PhoneUnavailable)


def test_battery_from_worker_thread_of_running_loop_works(monkeypatch):
    """The documented escape hatch -- run_in_executor -- must work normally."""
    pm = PhoneManager("com.example")
    monkeypatch.setattr(pm, "_diagnostics_battery", lambda: {"CurrentCapacity": 88, "Temperature": 3200, "IsCharging": True})

    async def call_via_executor():
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, pm.battery)

    b = asyncio.run(call_via_executor())
    assert b["level"] == 88 and b["charging"] is True


def test_run_bounds_every_call_with_a_finite_timeout(monkeypatch):
    """A call that never completes must not be able to hang the health tick forever."""
    pm = PhoneManager("com.example")
    monkeypatch.setattr(phone, "TIMEOUT_S", 0.05)

    async def hang():
        await phone.asyncio.sleep(10)

    with pytest.raises(TimeoutError):
        pm._run(hang())


def test_unavailable_errors_keep_the_cause_type():
    """Several pymobiledevice3 failures carry no message, and a bare
    "battery unavailable:" tells whoever is debugging nothing."""
    pm = PhoneManager("com.example.app")

    def boom():
        raise ConnectionAbortedError()

    with pytest.raises(PhoneUnavailable) as excinfo:
        pm._call(boom)
    assert "ConnectionAbortedError" in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, ConnectionAbortedError)


def test_battery_reports_power_figures_from_the_diagnostics(monkeypatch):
    """The keys came from a real device dump; without the current and the
    adapter wattage there is no way to see an underpowered port."""
    pm = PhoneManager("com.example.app")
    monkeypatch.setattr(pm, "_diagnostics_battery", lambda: {
        "CurrentCapacity": 71, "Temperature": 3739, "IsCharging": True,
        "InstantAmperage": 322, "Amperage": 250,
        "AdapterDetails": {"Description": "usb host", "Watts": 3, "Current": 500},
        "AppleRawMaxCapacity": 2167, "DesignCapacity": 2701,
    })
    b = pm.battery()
    assert b["level"] == 71
    assert b["temperature"] == 37.39
    assert b["charging"] is True
    assert b["current_ma"] == 322          # InstantAmperage preferred over Amperage
    assert b["charger_watts"] == 3
    assert b["health_pct"] == 80.2         # 2167 / 2701


def test_battery_tolerates_a_device_that_omits_the_power_fields(monkeypatch):
    pm = PhoneManager("com.example.app")
    monkeypatch.setattr(pm, "_diagnostics_battery", lambda: {"CurrentCapacity": 50})
    b = pm.battery()
    assert b["level"] == 50 and b["charger_watts"] is None and b["health_pct"] is None
