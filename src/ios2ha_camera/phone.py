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

TIMEOUT_S = 15  # finite and short: a hung call must never stall the supervisor's health tick
# auto_mount may need to download the DeveloperDiskImage over the network the first time it
# runs after a reboot; that can take much longer than a plain DVT call, so the mount step
# alone gets a wider bound than everything else in this module.
DDI_TIMEOUT_S = 120


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
        self._ddi_mounted = False

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

    def _mount_ddi(self):
        """Mount the DeveloperDiskImage needed by is_running/launch/screenshot. A no-op once
        this instance has mounted successfully; battery() never calls this at all."""
        if self._ddi_mounted:
            return

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
        self._ddi_mounted = True

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
        return {
            "level": d.get("CurrentCapacity"),
            "temperature": (d.get("Temperature") or 0) / 100.0,
            "charging": bool(d.get("IsCharging")),
        }

    def _proclist(self):
        async def go():
            from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
            from pymobiledevice3.services.dvt.instruments.device_info import DeviceInfo
            lockdown = await self._lockdown()
            try:
                async with DvtProvider(lockdown) as provider:
                    device_info = DeviceInfo(provider)
                    await device_info.connect()
                    return await device_info.proclist()
            finally:
                await lockdown.close()
        return self._run(go())

    def is_running(self):
        self._call(self._mount_ddi)
        procs = self._call(self._proclist)
        needle = self.bundle_id.split(".")[-1].lower()
        return any(needle in p.get("name", "").lower() for p in procs)

    def _launch(self):
        async def go():
            from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
            from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl
            lockdown = await self._lockdown()
            try:
                async with DvtProvider(lockdown) as provider:
                    process_control = ProcessControl(provider)
                    await process_control.connect()
                    return await process_control.launch(self.bundle_id)
            finally:
                await lockdown.close()
        return self._run(go())

    def launch(self):
        self._call(self._mount_ddi)
        pid = self._call(self._launch)
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
        self._call(self._mount_ddi)
        return self._call(self._take_screenshot)
