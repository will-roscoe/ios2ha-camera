"""Bring the phone back after it restarts, then wait for the one manual step.

Runs on arcturus, in the service's own image, and works while the container is
stopped:

    ./deploy.sh phone

Streaming can only be started from inside Guardian Eye, so this does everything
else and then waits, polling the app until frames appear.
"""

import argparse
import logging
import os
import sys
import time

from .phone import PhoneManager
from .settings import Settings
from .upstream import ProbeStreamingOff, http_probe

DEFAULT_BUNDLE_ID = "com.guardianeye.webcam.ios"


def main(argv=None):
    p = argparse.ArgumentParser(description="Prepare the phone after a restart")
    p.add_argument("--no-wait", action="store_true", help="skip waiting for Start streaming")
    p.add_argument("--timeout", type=float, default=300.0, help="how long to wait, in seconds")
    p.add_argument("--fix-developer-mode", action="store_true",
                   help="turn Developer Mode on if it is off. THIS REBOOTS THE PHONE")
    args = p.parse_args(argv)
    logging.basicConfig(level="INFO", format="%(message)s")

    s = Settings.from_env(os.environ)
    phone = PhoneManager(os.environ.get("PHONE_BUNDLE_ID", DEFAULT_BUNDLE_ID))

    if args.fix_developer_mode and not phone.developer_mode():
        print("enabling Developer Mode; the phone will reboot")
        phone.enable_developer_mode()

    failed = False
    for step in phone.prepare(launch=True):
        print(f"{'ok  ' if step['ok'] else 'FAIL'} {step['step']}: {step['detail']}")
        failed = failed or not step["ok"]

    if args.no_wait:
        return 1 if failed else 0

    deadline = time.time() + args.timeout
    print("waiting for frames: open Guardian Eye on the phone and tap Start streaming")
    while time.time() < deadline:
        try:
            http_probe(s)
        except ProbeStreamingOff:
            pass
        except Exception:
            pass
        else:
            print("frames are flowing")
            # A failed step earlier (battery unreadable, app absent) is still
            # worth a non-zero exit even though frames turned up -- otherwise
            # an unattended caller reads a healthy phone that was not one.
            return 1 if failed else 0
        time.sleep(5)
    print("timed out waiting for frames", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
