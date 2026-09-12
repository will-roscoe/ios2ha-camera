"""pymobiledevice3 wrappers for reading the iPhone's battery and controlling the camera app.

The installed line is pymobiledevice3 11.x (pinned in pyproject.toml), whose device-facing
API is entirely asyncio-based and organised differently to the 4.x-era API these wrappers
were first drafted against: there is no synchronous `create_using_usbmux()`, no
`DvtSecureSocketProxyService`, and no synchronous service methods. Every private helper here
opens its own event loop with `asyncio.run` for exactly one operation (connect, do the one
thing, close) so PhoneManager's public methods stay plain synchronous calls for the
supervisor's health tick, and so a lockdown/DTX connection never has to survive across loops.

Every public method is safe to call with the cable unplugged or the phone locked: real
pymobiledevice3 exceptions (no device, not paired, developer image not mounted, DTX channel
refused) are caught broadly and re-raised as PhoneUnavailable, and every call is bounded by
TIMEOUT_S so a stuck phone can never hang the health tick.
"""

import asyncio

TIMEOUT_S = 15  # finite and short: a hung call must never stall the supervisor's health tick


class PhoneUnavailable(Exception):
    pass


class PhoneManager:
    def __init__(self, bundle_id):
        self.bundle_id = bundle_id

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
        try:
            d = self._diagnostics_battery() or {}
        except Exception as e:
            raise PhoneUnavailable(str(e))
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
        try:
            procs = self._proclist()
        except Exception as e:
            raise PhoneUnavailable(str(e))
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
        try:
            pid = self._launch()
        except Exception as e:
            raise PhoneUnavailable(str(e))
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
        try:
            return self._take_screenshot()
        except Exception as e:
            raise PhoneUnavailable(str(e))
