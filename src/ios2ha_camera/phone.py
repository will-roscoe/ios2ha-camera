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
pymobiledevice3 exceptions (no device, not paired, developer image not mounted, DTX channel
refused) are caught broadly and re-raised as PhoneUnavailable, and every call is bounded by
TIMEOUT_S so a stuck phone can never hang the health tick. PhoneManagerMisuseError is a
distinct, separate exception from PhoneUnavailable -- it is a caller-side wiring mistake, not
a phone-side condition, and must never be mistaken for one.
"""

import asyncio

TIMEOUT_S = 15  # finite and short: a hung call must never stall the supervisor's health tick


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
            raise PhoneUnavailable(str(e))

    def _run(self, coro):
        return asyncio.run(asyncio.wait_for(coro, TIMEOUT_S))

    async def _lockdown(self):
        from pymobiledevice3.lockdown import create_using_usbmux
        # autopair=False: the phone is paired once by hand. An unattended health tick must
        # never sit waiting on the on-device "Trust This Computer?" dialog -- if the pairing
        # is ever lost this raises (caught below) instead of hanging.
        return await create_using_usbmux(autopair=False)

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
        return self._call(self._take_screenshot)
