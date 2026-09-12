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
    monkeypatch.setattr(pm, "_proclist", lambda: [{"name": "GuardianEye"}, {"name": "SpringBoard"}])
    assert pm.is_running() is True

def test_is_running_false_when_absent(monkeypatch):
    pm = PhoneManager("com.example.guardianeye")
    monkeypatch.setattr(pm, "_proclist", lambda: [{"name": "SpringBoard"}])
    assert pm.is_running() is False

def test_is_running_raises_when_no_device(monkeypatch):
    pm = PhoneManager("com.example")
    def boom(): raise RuntimeError("no device")
    monkeypatch.setattr(pm, "_proclist", boom)
    with pytest.raises(PhoneUnavailable):
        pm.is_running()

def test_launch_true_when_pid_returned(monkeypatch):
    pm = PhoneManager("com.example")
    monkeypatch.setattr(pm, "_launch", lambda: 4242)
    assert pm.launch() is True

def test_launch_raises_when_no_device(monkeypatch):
    pm = PhoneManager("com.example")
    def boom(): raise RuntimeError("no device")
    monkeypatch.setattr(pm, "_launch", boom)
    with pytest.raises(PhoneUnavailable):
        pm.launch()

def test_screenshot_returns_bytes(monkeypatch):
    pm = PhoneManager("com.example")
    monkeypatch.setattr(pm, "_take_screenshot", lambda: b"\x89PNG...")
    assert pm.screenshot() == b"\x89PNG..."

def test_screenshot_raises_when_developer_image_not_mounted(monkeypatch):
    """DDI mount does not survive a phone reboot -- must surface as PhoneUnavailable, not crash."""
    pm = PhoneManager("com.example")
    def boom(): raise RuntimeError("developer image not mounted")
    monkeypatch.setattr(pm, "_take_screenshot", boom)
    with pytest.raises(PhoneUnavailable):
        pm.screenshot()

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
