"""Read the environment, wire everything together, and run until stopped."""

import logging
import os
import threading

from aiohttp import web

from .archive import Archive
from .ingest import Ingestor
from .live import make_live_app
from .mqtt_io import MqttIO
from .onvif import OnvifClient
from .phone import PhoneManager
from .ring_buffer import RingBuffer
from .settings import Settings
from .supervisor import Supervisor
from . import version

log = logging.getLogger(__name__)

DEFAULT_BUNDLE_ID = "com.guardianeye.webcam.ios"


def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    s = Settings.from_env(os.environ)
    buf = RingBuffer(int(os.environ.get("BUFFER_BYTES", 1 << 30)))
    archive = Archive(s.archive_dir)
    onvif = OnvifClient(s.phone_host, s.phone_port, s.phone_user, s.phone_password)
    phone = PhoneManager(os.environ.get("PHONE_BUNDLE_ID", DEFAULT_BUNDLE_ID))

    # Late-bound on purpose: MqttIO needs the supervisor for its callbacks and
    # the supervisor needs MqttIO to publish. Neither callback can fire before
    # connect() below, by which point `sup` exists.
    mqtt = MqttIO(s,
                  on_control=lambda n, v: sup.handle_control(n, v),
                  on_connected=lambda: sup.publish_controls())
    sup = Supervisor(s, buf, mqtt, archive, onvif=onvif, phone=phone)
    ing = Ingestor(s.phone_host, s.phone_port, s.phone_user, s.phone_password, buf,
                   on_state=sup.on_stream_state)

    stop = threading.Event()
    threads = [
        threading.Thread(target=sup.run_capture, args=(stop,), name="capture", daemon=True),
        threading.Thread(target=sup.run_health, args=(stop,), name="health", daemon=True),
    ]

    mqtt.connect()      # asynchronous: a broker that is down does not stop capture
    mqtt.loop_start()
    ing.start()
    for t in threads:
        t.start()
    # Version first, and on its own line: "what is actually running" is the
    # first question asked when an unattended service misbehaves.
    log.info("ios2ha-camera %s, built %s", version.full(), version.BUILT)
    log.info("supervisor up: phone %s:%s, live view on :%s, interval %ss, archive %s",
             s.phone_host, s.phone_port, s.live_port, s.interval_s, s.archive_dir)

    # The event loop belongs to the live view alone. run_app returns on SIGTERM,
    # which is how the container stops us.
    try:
        web.run_app(make_live_app(buf, latest_still=lambda: sup.last_still_jpeg),
                    host="0.0.0.0", port=s.live_port, print=None)
    finally:
        log.info("shutting down")
        stop.set()
        sup.capture_requested.set()   # wake the capture loop out of its wait
        ing.stop()
        sup.close()
        for t in threads:
            t.join(timeout=5)
        mqtt.close()                  # publishes offline; a clean disconnect has no last will


if __name__ == "__main__":
    main()
