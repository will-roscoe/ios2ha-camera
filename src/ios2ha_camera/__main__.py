"""Read the environment, wire everything together, and run until stopped."""

import logging
import os
import threading

from aiohttp import web

from . import version
from .app_open import AppOpener
from .archive import Archive
from .ingest import Ingestor
from .keyboard import Keyboard
from .live import make_live_app
from .live_stack import LiveStacker
from .mqtt_io import MqttIO
from .onvif import OnvifClient
from .phone import PhoneManager
from .ring_buffer import RingBuffer
from .settings import Settings
from .stream_start import StreamStarter
from .supervisor import Supervisor
from .upstream import ProbeStalled, ProbeStreamingOff, Upstream, http_probe

log = logging.getLogger(__name__)

DEFAULT_BUNDLE_ID = "com.guardianeye.webcam.ios"


def _stream_probe(settings):
    """The app's own answer, in the three words StreamStarter reasons about."""
    def probe():
        try:
            http_probe(settings)
            return "streaming"          # frames are flowing
        except ProbeStreamingOff:
            return "off"                # 503: open, Start streaming not tapped
        except ProbeStalled:
            # A frame then silence: the app believes it is streaming, and keys
            # would stop it. Not something this step can fix.
            return "streaming"
        except Exception:
            return "unreachable"        # refused or timed out: backgrounded, locked, gone
    return probe


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
    ing = Ingestor(s.phone_host, s.phone_port, s.phone_user, s.phone_password, buf)
    # Lambdas, not bound methods: `sup` does not exist yet (see the note above),
    # and a bound method would be looked up now rather than at call time.
    up = Upstream(ing, buf, s, on_state=lambda st: sup.on_stream_state(st),
                  on_session_open=lambda: sup.on_session_open(),
                  on_session_close=lambda: sup.on_session_close())
    ing.on_state = up.on_ingest_state
    keyboard = Keyboard()
    log.info("keyboard: %s", keyboard.start())
    starter = StreamStarter(_stream_probe(s), keyboard, phone.is_running,
                            allowed=lambda: bool(getattr(s, "auto_start", True)),
                            foreground=phone.foreground)
    opener = AppOpener(phone.is_running, phone.launch,
                       allowed=lambda: bool(getattr(s, "auto_start", True)))
    sup = Supervisor(s, buf, mqtt, archive, onvif=onvif, phone=phone, upstream=up,
                     starter=starter, keyboard=keyboard, opener=opener)
    stacker = LiveStacker(buf, s)
    sup.live_fps = stacker.fps

    stop = threading.Event()
    threads = [
        threading.Thread(target=sup.run_capture, args=(stop,), name="capture", daemon=True),
        threading.Thread(target=sup.run_health, args=(stop,), name="health", daemon=True),
    ]

    mqtt.connect()      # asynchronous: a broker that is down does not stop capture
    mqtt.loop_start()
    for t in threads:
        t.start()
    # Version first, and on its own line: "what is actually running" is the
    # first question asked when an unattended service misbehaves.
    log.info("ios2ha-camera %s, built %s", version.full(), version.BUILT)
    log.info("supervisor up: phone %s:%s, live view on :%s, interval %ss, archive %s",
             s.phone_host, s.phone_port, s.live_port, s.interval_s, s.archive_dir)
    # Started last, after the log lines above: its first tick can fire a probe
    # immediately, which must not race ahead of them in the log.
    up.start()

    # The event loop belongs to the live view alone. run_app returns on SIGTERM,
    # which is how the container stops us.
    try:
        web.run_app(make_live_app(buf, latest_still=lambda: sup.last_still_jpeg,
                                  upstream=up, stacker=stacker),
                    host="0.0.0.0", port=s.live_port, print=None,
                    # A /live viewer never finishes by itself, and at aiohttp's
                    # default of 60 s the container is killed before the block
                    # below ever runs.
                    shutdown_timeout=2.0)
    finally:
        log.info("shutting down")
        stop.set()
        sup.capture_requested.set()   # wake the capture loop out of its wait
        # Compose allows 20 s from SIGTERM. The event loop's shutdown above has
        # already spent up to 4 s of it, not 2: aiohttp waits shutdown_timeout
        # once for handlers to finish and again after cancelling them, and a
        # /live viewer waiting for a frame sits out both (measured 4.0 s). Then
        # 5 s for up.stop(), two 3 s thread joins and up to 2 s for mqtt.close()
        # -- 17 s in all. An upstream thread still alive after its 5 s is stuck
        # in a probe, which stop() deliberately leaves alone rather than
        # touching the ingestor from a second thread.
        up.stop(timeout=5.0)
        # Its thread is a daemon, so this is tidiness rather than necessity: it
        # unregisters the advertisement instead of leaving BlueZ to notice.
        keyboard.stop(timeout=3.0)
        sup.close()
        for t in threads:
            t.join(timeout=3)
        mqtt.close()                  # publishes offline; a clean disconnect has no last will


if __name__ == "__main__":
    main()
