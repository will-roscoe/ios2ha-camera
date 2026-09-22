"""pymobiledevice3 wrappers for reading the iPhone's battery and controlling the camera app.

The installed line is pymobiledevice3 11.x (pinned in pyproject.toml), whose device-facing
API is entirely asyncio-based and organised differently to the 4.x-era API these wrappers
were first drafted against: there is no synchronous `create_using_usbmux()`, no
`DvtSecureSocketProxyService`, and no synchronous service methods. Every private helper here
opens its own event loop with `asyncio.run` for exactly one operation (connect, do the one
thing, close), so a lockdown/DTX connection never has to survive across loops.

Every PhoneManager public method is a BLOCKING call: it runs `asyncio.run` to completion on
the calling thread. That is safe from a plain thread (a synchronous health-tick loop, a
worker thread) but is an error when called from a coroutine already running on an event
loop -- `asyncio.run` cannot nest. Calling from inside a running loop raises
PhoneManagerMisuseError immediately, before any device I/O; from an aiohttp-based (or any
other async) supervisor, call these methods via `loop.run_in_executor(None, phone.battery)`
instead of awaiting them directly.

Every public method is also safe to call with the cable unplugged or the phone locked: real
pymobiledevice3 exceptions (no device, not paired, DTX channel refused) are caught broadly
and re-raised as PhoneUnavailable, and every call is bounded by a finite timeout so a stuck
phone can never hang the health tick. PhoneManagerMisuseError is a distinct, separate
exception from PhoneUnavailable -- it is a caller-side wiring mistake, not a phone-side
condition, and must never be mistaken for one.

The DeveloperDiskImage does not survive a phone reboot (spec 4.7), so `is_running`, `launch`
and `screenshot` -- the three calls that go over the DVT/DTX channel -- mount it on demand,
lazily and once per PhoneManager instance: skipped once a mount has succeeded, retried on
every call after a failure (a reboot is exactly the case a retry should self-heal from).
`battery()` never mounts anything -- DiagnosticsService is a plain lockdown service and
needs no DDI, so it stays available even on a freshly rebooted, unmounted phone.
"""

import asyncio
import threading

TIMEOUT_S = 15  # finite and short: a hung call must never stall the supervisor's health tick
# auto_mount may need to download the DeveloperDiskImage over the network the first time it
# runs after a reboot; that can take much longer than a plain DVT call, so the mount step
# alone gets a wider bound than everything else in this module.
DDI_TIMEOUT_S = 120


# The app foregrounded in order to background the camera app. Settings is the
# safest choice available on every iPhone: it is already installed, does nothing
# on its own, uses no camera or network, and is harmless to leave on screen --
# especially with Screen Curtain on, which is how this phone runs.
BACKGROUND_BUNDLE_ID = "com.apple.Preferences"


class PhoneUnavailable(Exception):
    pass


class PhoneManagerMisuseError(RuntimeError):
    """A PhoneManager method was called from inside a running event loop.

    Every public method blocks the calling thread (see module docstring). Call it from a
    plain thread instead -- e.g. `await loop.run_in_executor(None, phone.battery)` from a
    coroutine, not `phone.battery()` directly.
    """


class PhoneManager:
    def __init__(self, bundle_id):
        self.bundle_id = bundle_id
        # Filled from the phone's own app list on first use; see executable_name.
        self._executable = None
        # iOS serves one instruments session at a time. Two threads each opening
        # their own -- the health tick asking whether the app is running, and the
        # step that opens it doing the same -- leave both hanging until their
        # timeouts, and the phone's side stays wedged afterwards: measured on
        # 2026-09-16, the camera was dark for two and a half hours with a healthy
        # phone on the end of the cable, and only restarting the container (which
        # drops the sessions) cleared it. Reentrant because one call may be built
        # from another, as is_running is from the app list lookup.
        self._dvt_lock = threading.RLock()
        self._ddi_mounted = False
        # Consecutive failures of calls that need the developer services. One of
        # these wedged every such call for 24 hours on 2026-09-14 while the
        # supervisor kept logging and nothing recovered, so it is published.
        self.dvt_failures = 0

    def _call(self, fn):
        """Run fn(), guarding against the running-event-loop misuse case and converting any
        other failure to PhoneUnavailable. The guard sits outside the try/except below on
        purpose: PhoneManagerMisuseError must propagate as itself, never get relabelled as
        PhoneUnavailable, so a caller-side wiring mistake fails loudly instead of looking
        exactly like a permanently unreachable phone."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass  # no event loop running on this thread -- safe to block here
        else:
            raise PhoneManagerMisuseError(
                "PhoneManager methods block the calling thread and must not be called from "
                "inside a running event loop; call from a worker thread instead, e.g. "
                "await loop.run_in_executor(None, phone_manager.battery)."
            )
        try:
            return fn()
        except Exception as e:
            # Keep the exception type: several pymobiledevice3 failures carry no
            # message at all, and a log line reading "battery unavailable:" with
            # nothing after the colon is no use when diagnosing unattended.
            detail = str(e).strip()
            raise PhoneUnavailable(
                f"{type(e).__name__}: {detail}" if detail else type(e).__name__
            ) from e

    def _run(self, coro, timeout=None):
        return asyncio.run(asyncio.wait_for(coro, TIMEOUT_S if timeout is None else timeout))

    async def _lockdown(self):
        from pymobiledevice3.lockdown import create_using_usbmux
        # autopair=False: the phone is paired once by hand. An unattended health tick must
        # never sit waiting on the on-device "Trust This Computer?" dialog -- if the pairing
        # is ever lost this raises (caught below) instead of hanging.
        return await create_using_usbmux(autopair=False)

    async def _auto_mount(self, lockdown):
        from pymobiledevice3.services.mobile_image_mounter import auto_mount
        await auto_mount(lockdown)

    def _dvt(self, fn):
        """Run a call that needs the developer disk image, mounting it first.

        A failure clears the mount cache and counts: on 2026-09-14 one
        ConnectionTerminatedError left every later developer call failing for
        24 hours, because the mount was cached for the life of the process and
        never re-checked. Backgrounding stopped, the app stayed streaming, and
        the battery died. Whether the phone or the process was wedged is still
        unknown, so this assumes neither -- it simply stops trusting the cache.
        """
        with self._dvt_lock:
            try:
                self._call(self._mount_ddi)
                result = self._call(fn)
            except Exception:
                # The mount step itself is a DVT failure too -- a wedged mount is the
                # exact shape of the 2026-09-14 outage, and dvt_failures must climb for
                # it just like it does for a failure of fn() after a successful mount.
                self._ddi_mounted = False
                self.dvt_failures += 1
                raise
            self.dvt_failures = 0
            return result

    def _mount_ddi(self):
        """Mount the DeveloperDiskImage needed by is_running/launch/screenshot. A no-op once
        this instance has mounted successfully; battery() never calls this at all."""
        if self._ddi_mounted:
            return
        self._mount_ddi_now()
        self._ddi_mounted = True

    def _mount_ddi_now(self):
        """Mount it, cache or no cache."""

        async def go():
            from pymobiledevice3.services.mobile_image_mounter import AlreadyMountedError
            lockdown = await self._lockdown()
            try:
                try:
                    await self._auto_mount(lockdown)
                except AlreadyMountedError:
                    pass  # already mounted -- the normal case on a phone that has not rebooted
            finally:
                await lockdown.close()

        self._run(go(), timeout=DDI_TIMEOUT_S)

    def _diagnostics_battery(self):
        async def go():
            from pymobiledevice3.services.diagnostics import DiagnosticsService
            lockdown = await self._lockdown()
            try:
                return await DiagnosticsService(lockdown).get_battery()
            finally:
                await lockdown.close()
        return self._run(go())

    def battery(self):
        d = self._call(self._diagnostics_battery) or {}
        adapter = d.get("AdapterDetails") or {}
        raw_max, design = d.get("AppleRawMaxCapacity"), d.get("DesignCapacity")
        return {
            "level": d.get("CurrentCapacity"),
            "temperature": (d.get("Temperature") or 0) / 100.0,
            "charging": bool(d.get("IsCharging")),
            # Signed, and the figure IsCharging cannot give you: the phone
            # reports charging while still losing ground on an underpowered
            # port, so only the current says which way the battery is going.
            "current_ma": d.get("InstantAmperage", d.get("Amperage")),
            # What the port actually offers. A plain USB host port advertises
            # 3 W, which is marginal for a phone capturing continuously.
            "charger_watts": adapter.get("Watts"),
            # Maximum against design capacity. An aged cell has higher internal
            # resistance, so it runs hotter for the same work.
            "health_pct": round(100 * raw_max / design, 1) if raw_max and design else None,
        }

    def _diagnostics_restart(self):
        async def go():
            from pymobiledevice3.services.diagnostics import DiagnosticsService
            lockdown = await self._lockdown()
            try:
                return await DiagnosticsService(lockdown).restart()
            finally:
                await lockdown.close()
        return self._run(go())

    def reboot(self):
        """Restart the phone. The only known cure for wedged developer services.

        Deliberately a plain lockdown call, like `battery()` and unlike anything
        routed through `_dvt`: when the DeveloperDiskImage mounter is what hangs,
        a recovery that needed the image could never run. Proven 2026-09-22,
        which cleared a wedge four days old.

        The phone comes back with the app backgrounded and the image unmounted,
        both of which the existing steps handle: `_dvt` re-mounts on the next
        call and `AppOpener` fronts the app.
        """
        self._ddi_mounted = False
        self._call(self._diagnostics_restart)

    def dvt_ok(self):
        """Whether a developer call works right now, without changing anything.

        `is_running` is the cheapest true DVT round trip: it mounts the image
        first, like everything else in `_dvt`, so it answers for both halves of
        the wedge -- the mounter that hangs and the handshake that is refused.
        """
        try:
            self.is_running()
        except Exception:
            return False
        return True

    def _proclist(self):
        async def go():
            from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
            from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
            lockdown = await self._lockdown()
            try:
                async with DvtProvider(lockdown) as provider:
                    device_info = DeviceInfo(provider)
                    await device_info.connect()
                    return await device_info.proclist()
            finally:
                await lockdown.close()
        return self._run(go())

    def _applist(self):
        async def go():
            from pymobiledevice3.services.dvt.instruments.application_listing import (
                ApplicationListing,
            )
            from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
            lockdown = await self._lockdown()
            try:
                async with DvtProvider(lockdown) as provider:
                    listing = ApplicationListing(provider)
                    await listing.connect()
                    return await listing.applist()
            finally:
                await lockdown.close()
        return self._run(go())

    def executable_name(self):
        """The app's executable, asked of the phone once and remembered.

        Worth the extra round trip, because guessing it from the bundle id is
        wrong in a way that hides: taking the last word of
        `com.guardianeye.webcam.ios` gives "ios", which matches `apfs_iosd` on
        every iPhone, so "is the app running" answered yes on a phone where the
        app was not even installed. Asking the device also means swapping the
        phone, or the app, needs no change here.
        """
        if self._executable is None:
            for app in self._dvt(self._applist) or []:
                if app.get("CFBundleIdentifier") == self.bundle_id:
                    self._executable = app.get("ExecutableName") or app.get("CFBundleExecutable")
                    break
        if not self._executable:
            raise PhoneUnavailable(f"{self.bundle_id} is not installed on this phone")
        return self._executable

    def is_running(self):
        name = self.executable_name().lower()
        procs = self._dvt(self._proclist)
        return any(p.get("name", "").lower() == name for p in procs)

    def _launch(self, bundle_id=None, kill_existing=True):
        bundle_id = bundle_id or self.bundle_id

        async def go():
            from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
            from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
            lockdown = await self._lockdown()
            try:
                async with DvtProvider(lockdown) as provider:
                    process_control = ProcessControl(provider)
                    await process_control.connect()
                    return await process_control.launch(bundle_id, kill_existing=kill_existing)
            finally:
                await lockdown.close()
        return self._run(go())

    def launch(self):
        """Cold-start the app, killing any running copy.

        Note what this costs: a cold start always leaves **streaming off**, so
        the stream comes back only once `StreamStarter` presses Start streaming
        (or a person does). That is the right behaviour for the relaunch button,
        whose whole purpose is reviving a dead app, and the wrong behaviour for
        merely bringing a running app back to the front -- use `foreground()`.
        """
        pid = self._dvt(lambda: self._launch(kill_existing=True))
        return bool(pid)

    def foreground(self):
        """Bring the app to the front **without restarting it**.

        `kill_existing=False` is the whole point. pymobiledevice3 defaults it to
        True, which kills the running copy and cold-starts a new one -- and a
        cold start leaves streaming off, needing someone to tap Start streaming.
        Measured on 2026-09-13: calling launch() with the default on a
        backgrounded-but-streaming app opened its port within 2 s and then
        answered 503, having silently thrown the stream away.
        """
        pid = self._dvt(lambda: self._launch(kill_existing=False))
        return bool(pid)

    def background(self, bundle_id=BACKGROUND_BUNDLE_ID):
        """Send the app to the background by foregrounding something else.

        There is no "go home" over usbmux: SpringBoardServicesService exposes
        only icon and wallpaper state, and killing the app would need a manual
        tap to recover. Foregrounding another app is what remains, and iOS then
        suspends this one's listening socket while leaving the process alive --
        which is the pause worth roughly 370 mA.

        `kill_existing=False` here too, so the decoy app is merely raised rather
        than restarted each time.
        """
        pid = self._dvt(lambda: self._launch(bundle_id, kill_existing=False))
        return bool(pid)

    def _take_screenshot(self):
        async def go():
            from pymobiledevice3.services.screenshot import ScreenshotService
            lockdown = await self._lockdown()
            try:
                return await ScreenshotService(lockdown).take_screenshot()
            finally:
                await lockdown.close()
        return self._run(go())

    def screenshot(self):
        return self._dvt(self._take_screenshot)

    def mount_ddi(self):
        """Public form of the lazy developer-image mount, for the setup helper."""
        self._call(self._mount_ddi)

    def _developer_mode(self):
        async def go():
            lockdown = await self._lockdown()
            try:
                return await lockdown.get_developer_mode_status()
            finally:
                await lockdown.close()
        return self._run(go())

    def developer_mode(self):
        return bool(self._call(self._developer_mode))

    def _enable_developer_mode(self):
        async def go():
            from pymobiledevice3.services.amfi import AmfiService
            lockdown = await self._lockdown()
            try:
                return await AmfiService(lockdown).enable_developer_mode()
            finally:
                await lockdown.close()
        return self._run(go(), timeout=DDI_TIMEOUT_S)

    def enable_developer_mode(self):
        """Turn Developer Mode on. REBOOTS THE PHONE, and raises if a passcode
        is set, so nothing calls this without a person asking for it."""
        return self._call(self._enable_developer_mode)

    def _check_lockdown(self):
        async def go():
            lockdown = await self._lockdown()
            await lockdown.close()
        return self._run(go())

    def prepare(self, launch=True):
        """Bring the phone back to a state where it can stream, and say what it found.

        Every step runs even if an earlier one failed: "the developer image is
        missing" and "the app is not running" are separate facts, and the person
        reading this wants both. The one thing that cannot be automated is the
        Start streaming tap in the app.
        """
        steps = []

        def run(name, fn):
            try:
                steps.append({"step": name, "ok": True, "detail": fn()})
            except PhoneManagerMisuseError:
                # A caller-side wiring bug, not a phone-side condition: recording it
                # as a failed step would bury the exact thing _call is meant to
                # surface loudly, and every remaining step would fail identically.
                raise
            except Exception as e:
                steps.append({"step": name, "ok": False, "detail": f"{type(e).__name__}: {e}"})

        # First, because every later step goes over lockdown: with the cable out
        # or the pairing lost, that is the one fact that explains the rest.
        run("lockdown", lambda: (self._call(self._check_lockdown), "reachable")[1])
        run("developer_mode",
            lambda: "on" if self.developer_mode() else "OFF (run with --fix-developer-mode)")
        run("developer_image", lambda: (self.mount_ddi(), "mounted")[1])

        def app():
            if self.is_running():
                return "already running"
            if not launch:
                return "not running"
            self.launch()
            return "launched; tap Start streaming"
        run("app", app)
        run("battery", lambda: self.battery())
        return steps
