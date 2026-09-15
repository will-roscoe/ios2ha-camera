import asyncio
import time
import types

import pytest

from ios2ha_camera import phone, phone_setup
from ios2ha_camera.phone import (
    BACKGROUND_BUNDLE_ID,
    PhoneManager,
    PhoneManagerMisuseError,
    PhoneUnavailable,
)
from ios2ha_camera.upstream import ProbeStalled, ProbeStreamingOff


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
    monkeypatch.setattr(pm, "_launch", lambda *a, **k: 4242)
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
    monkeypatch.setattr(pm, "_launch", lambda *a, **k: 4242)
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


def test_prepare_reports_every_step_even_when_one_fails(monkeypatch):
    pm = PhoneManager("com.example.app")
    lockdown = _FakeLockdown()
    monkeypatch.setattr(pm, "_lockdown", _async_return(lockdown))
    monkeypatch.setattr(pm, "developer_mode", lambda: True)
    monkeypatch.setattr(pm, "mount_ddi", lambda: None)
    monkeypatch.setattr(pm, "is_running", lambda: (_ for _ in ()).throw(PhoneUnavailable("no DDI")))
    monkeypatch.setattr(pm, "launch", lambda: True)
    monkeypatch.setattr(pm, "battery", lambda: {"level": 48})
    steps = pm.prepare(launch=True)
    names = [s["step"] for s in steps]
    assert names == ["lockdown", "developer_mode", "developer_image", "app", "battery"]
    assert steps[0]["ok"] is True and lockdown.closed is True
    assert steps[3]["ok"] is False
    assert steps[4]["ok"] is True            # a failed step does not abort the rest


def test_prepare_never_enables_developer_mode(monkeypatch):
    pm = PhoneManager("com.example.app")
    called = []
    monkeypatch.setattr(pm, "_lockdown", _async_return(_FakeLockdown()))
    monkeypatch.setattr(pm, "developer_mode", lambda: False)
    monkeypatch.setattr(pm, "enable_developer_mode", lambda: called.append("enabled"))
    monkeypatch.setattr(pm, "mount_ddi", lambda: None)
    monkeypatch.setattr(pm, "is_running", lambda: True)
    monkeypatch.setattr(pm, "battery", lambda: {"level": 48})
    pm.prepare(launch=True)
    assert called == []                       # enabling reboots the phone: never automatic


def test_prepare_reraises_phone_manager_misuse_error(monkeypatch):
    """A caller-side wiring bug (prepare() called from inside a running event
    loop) must propagate untouched. Swallowing it here would misreport the
    exact condition _call is designed to surface loudly, as an ordinary
    phone-side failure -- and the three steps after it would fail identically
    for no new reason, adding noise instead of information."""
    pm = PhoneManager("com.example.app")
    monkeypatch.setattr(pm, "_lockdown", _async_return(_FakeLockdown()))
    monkeypatch.setattr(pm, "developer_mode",
                         lambda: (_ for _ in ()).throw(PhoneManagerMisuseError("wrong thread")))
    monkeypatch.setattr(pm, "mount_ddi", lambda: None)
    monkeypatch.setattr(pm, "is_running", lambda: True)
    monkeypatch.setattr(pm, "battery", lambda: {"level": 48})
    with pytest.raises(PhoneManagerMisuseError):
        pm.prepare(launch=True)


def test_main_reports_failure_even_when_frames_arrive(monkeypatch):
    """A failed prepare() step (e.g. the app not running) must not be masked
    by the probe finding frames from an earlier session -- the exit code is
    the one signal an unattended caller (deploy.sh, cron) reads, and
    returning 0 here would report a healthy phone that was not."""
    class FakePhone:
        def __init__(self, bundle_id):
            pass

        def prepare(self, launch=True):
            return [{"step": "app", "ok": False, "detail": "boom"},
                    {"step": "battery", "ok": True, "detail": {"level": 48}}]

    class FakeSettings:
        @staticmethod
        def from_env(env):
            return object()

    monkeypatch.setattr(phone_setup, "PhoneManager", FakePhone)
    monkeypatch.setattr(phone_setup, "Settings", FakeSettings)
    monkeypatch.setattr(phone_setup, "http_probe", lambda s: object())  # frames found immediately
    assert phone_setup.main(["--timeout", "1"]) == 1


def test_developer_mode_reports_what_lockdown_says(monkeypatch):
    class ModeLockdown(_FakeLockdown):
        def __init__(self, status):
            super().__init__()
            self.status = status

        async def get_developer_mode_status(self):
            return self.status

    for status in (True, False):
        pm = PhoneManager("com.example.app")
        lockdown = ModeLockdown(status)
        monkeypatch.setattr(pm, "_lockdown", _async_return(lockdown))
        assert pm.developer_mode() is status
        assert lockdown.closed is True


def _fake_setup(monkeypatch, developer_mode=True):
    calls = []

    class FakePhone:
        def __init__(self, bundle_id):
            pass

        def developer_mode(self):
            calls.append("developer_mode")
            return developer_mode

        def enable_developer_mode(self):
            calls.append("enable_developer_mode")

        def prepare(self, launch=True):
            return [{"step": "app", "ok": True, "detail": "already running"}]

    class FakeSettings:
        @staticmethod
        def from_env(env):
            return object()

    monkeypatch.setattr(phone_setup, "PhoneManager", FakePhone)
    monkeypatch.setattr(phone_setup, "Settings", FakeSettings)
    return calls


def test_the_cli_never_enables_developer_mode_without_the_flag(monkeypatch):
    """Enabling Developer Mode reboots the phone."""
    calls = _fake_setup(monkeypatch, developer_mode=False)
    assert phone_setup.main(["--no-wait"]) == 0
    assert "enable_developer_mode" not in calls
    calls = _fake_setup(monkeypatch, developer_mode=False)
    phone_setup.main(["--no-wait", "--fix-developer-mode"])
    assert calls == ["developer_mode", "enable_developer_mode"]


def test_the_wait_prints_each_new_kind_of_probe_failure_once(monkeypatch, capsys):
    """Every probe error used to be swallowed, so a wait that could never end
    well (a wrong password, the app gone) looked just like one waiting for a tap."""
    outcomes = [ConnectionRefusedError(111, "Connection refused"),
                ConnectionRefusedError(111, "Connection refused"),
                ProbeStreamingOff(), ProbeStreamingOff(),
                ProbeStalled(), ProbeStalled()]

    def probe(settings):
        if outcomes:
            raise outcomes.pop(0)
        return object()

    _fake_setup(monkeypatch)
    monkeypatch.setattr(phone_setup, "http_probe", probe)
    monkeypatch.setattr(phone_setup, "time",
                        types.SimpleNamespace(time=time.time, sleep=lambda s: None))
    assert phone_setup.main(["--timeout", "5"]) == 0
    out = capsys.readouterr().out
    waiting = [line for line in out.splitlines() if line.startswith("still waiting")]
    assert len(waiting) == 3, out
    assert "ConnectionRefusedError" in waiting[0]
    assert "503" in waiting[1]
    # A hung app used to pass the one-frame probe and read as "frames are flowing".
    assert "frozen" in waiting[2]
    assert "frames are flowing" in out


def test_a_dvt_failure_clears_the_mount_cache_so_the_next_call_remounts(monkeypatch):
    """On 2026-09-14 one ConnectionTerminatedError wedged every later developer
    call for 24 hours: backgrounding stopped, the app stayed streaming, and the
    battery died. The mount was cached for the life of the process and never
    retried, so nothing could recover without restarting the container."""
    pm = phone.PhoneManager("com.example.app")
    mounts = []
    monkeypatch.setattr(pm, "_mount_ddi_now", lambda: mounts.append(1))
    monkeypatch.setattr(pm, "_proclist",
                        lambda: (_ for _ in ()).throw(RuntimeError("ConnectionTerminatedError")))
    with pytest.raises(phone.PhoneUnavailable):
        pm.is_running()
    assert pm._ddi_mounted is False
    assert pm.dvt_failures == 1

    # The name has to contain the bundle id's last part ("app"), which is what
    # is_running matches on.
    monkeypatch.setattr(pm, "_proclist", lambda: [{"name": "ExampleApp", "pid": 1}])
    assert pm.is_running() is True
    assert len(mounts) == 2       # remounted rather than trusting the stale cache
    assert pm.dvt_failures == 0


def test_a_healthy_run_mounts_once(monkeypatch):
    """The cache still earns its keep: mounting on every call would spend the
    cable on a question whose answer almost never changes."""
    pm = phone.PhoneManager("com.example.app")
    mounts = []
    monkeypatch.setattr(pm, "_mount_ddi_now", lambda: mounts.append(1))
    monkeypatch.setattr(pm, "_proclist", lambda: [{"name": "ExampleApp", "pid": 1}])
    pm.is_running()
    pm.is_running()
    assert len(mounts) == 1


# -- foreground vs relaunch, which is not a cosmetic distinction ----------------

def _record_launch(monkeypatch, pm):
    """Capture what launch() is actually asked to do."""
    calls = []
    monkeypatch.setattr(pm, "_mount_ddi", lambda: None)
    monkeypatch.setattr(pm, "_launch",
                        lambda bundle_id=None, kill_existing=True:
                        (calls.append((bundle_id, kill_existing)), 4242)[1])
    return calls


def test_foreground_does_not_kill_the_running_app(monkeypatch):
    """The whole point of foreground(). pymobiledevice3 defaults kill_existing
    to True, which cold-starts the app -- and a cold start leaves streaming off,
    needing someone to tap Start streaming. Measured on the device 2026-09-13:
    the default silently threw a live stream away."""
    pm = PhoneManager("com.example")
    calls = _record_launch(monkeypatch, pm)
    assert pm.foreground() is True
    assert calls == [(None, False)], "foreground must not kill the running app"


def test_launch_still_cold_starts(monkeypatch):
    """The relaunch button revives a dead app, so it must kill any zombie copy."""
    pm = PhoneManager("com.example")
    calls = _record_launch(monkeypatch, pm)
    assert pm.launch() is True
    assert calls == [(None, True)]


def test_background_foregrounds_a_different_app(monkeypatch):
    """There is no "go home" over usbmux, so backgrounding means raising
    something else. It must not be the camera app, or nothing happens."""
    pm = PhoneManager("com.example")
    calls = _record_launch(monkeypatch, pm)
    assert pm.background() is True
    (bundle, kill_existing), = calls
    assert bundle == BACKGROUND_BUNDLE_ID
    assert bundle != pm.bundle_id
    assert kill_existing is False


def test_background_accepts_a_different_decoy(monkeypatch):
    pm = PhoneManager("com.example")
    calls = _record_launch(monkeypatch, pm)
    pm.background("com.apple.mobiletimer")
    assert calls == [("com.apple.mobiletimer", False)]
