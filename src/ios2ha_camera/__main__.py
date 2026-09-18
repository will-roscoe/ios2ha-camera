"""Read the environment, wire everything together, and run until stopped."""

import logging
import os
import threading

from aiohttp import web

from . import version
from .api import INFO, add_api_routes
from .app_open import AppOpener
from .archive import Archive
from .events import EventHub
from .ingest import Ingestor
from .keyboard import Keyboard
from .live import make_live_app
from .live_stack import LiveStacker
from .mqtt_io import MqttIO, NullMqttIO
from .onvif import OnvifClient
from .phone import PhoneManager
from .ring_buffer import RingBuffer
from .settings import Settings, both_interfaces_off_message
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


def _mqtt_for(s, on_control=lambda n, v: None, on_connected=lambda: None):
    """The object the supervisor publishes its state, events and stills to.

    With MQTT switched off that is a stand-in which discards them, and the
    broker settings are not read at all -- so a stale or malformed one cannot
    stop a deployment that no longer uses it from starting.
    """
    if not s.expose_mqtt:
        return NullMqttIO()
    return MqttIO(s, on_control=on_control, on_connected=on_connected)


def _clear_discovery(s):
    """Remove the retained discovery topics, so Home Assistant forgets the
    entities rather than keeping unavailable ghosts of them and their ids.

    The client is built here rather than at startup because its constructor
    rejects a malformed broker URL, and switching MQTT off must not become a way
    to crash on a setting that is no longer in use. The attempt itself is
    bounded inside teardown_discovery.
    """
    try:
        MqttIO(s).teardown_discovery()
    except Exception:
        log.warning("could not clear the MQTT discovery topics", exc_info=True)


def _start_interfaces(s, sup, app, mqtt):
    """Bring up whichever of the two Home Assistant interfaces are switched on,
    say so once each, and return the event hub if there is one.

    Neither may stop the service, which is the point of having the flags at all:
    an interface that is off costs one log line and leaves nothing behind -- no
    connection, no routes, and above all no retry. The media routes are already
    on `app` and are never gated: they are the camera.

    Kept out of main(), which runs the service forever, so that each combination
    of the two flags can be exercised without starting it.
    """
    # The supervisor remembers the values it publishes, and that memory is what
    # the JSON API answers from, so a value nobody has published yet reads as
    # absent. Reaching the broker is what normally publishes the controls for
    # the first time, which leaves them absent whenever the broker is down and
    # for good when MQTT is off entirely. Publishing them here costs one pass
    # over the settings and makes the API complete from its first request.
    sup.publish_controls()

    if s.expose_mqtt:
        mqtt.connect()      # asynchronous: a broker that is down does not stop capture
        mqtt.loop_start()
        log.info("MQTT discovery: on")
    else:
        log.info("MQTT discovery: off, clearing discovery topics")
        # On a daemon thread: clearing makes one bounded attempt to reach a
        # broker that may not exist at all, and on this thread that wait would
        # hold up capture, health and the live view for as long as it took.
        threading.Thread(target=_clear_discovery, args=(s,),
                         name="mqtt-teardown", daemon=True).start()

    hub = None
    if s.expose_api:
        # The stream is fed from the same call MQTT is fed from, so the two
        # cannot end up reporting different values for the same key.
        hub = EventHub()
        sup.add_state_listener(hub.publish)
        add_api_routes(app, sup.current_state, s, events=hub, on_control=sup.handle_control)
        log.info("JSON API: on at %s", INFO)
    else:
        log.info("JSON API: off")

    if not s.expose_mqtt and not s.expose_api:
        log.warning(both_interfaces_off_message())
    return hub


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
    # the connect in _start_interfaces below, by which point `sup` exists.
    mqtt = _mqtt_for(s,
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
    # Never while the battery policy has deliberately backgrounded the app:
    # that pause is meant to be held, and the policy foregrounds it itself for
    # the next capture. `sup` is late-bound here for the reason given above.
    opener = AppOpener(phone.is_running, phone.launch, foreground=phone.foreground,
                       allowed=lambda: (bool(getattr(s, "auto_start", True))
                                        and not sup.backgrounding.backgrounded))
    sup = Supervisor(s, buf, mqtt, archive, onvif=onvif, phone=phone, upstream=up,
                     starter=starter, keyboard=keyboard, opener=opener)
    stacker = LiveStacker(buf, s)
    sup.live_fps = stacker.fps

    stop = threading.Event()
    threads = [
        threading.Thread(target=sup.run_capture, args=(stop,), name="capture", daemon=True),
        threading.Thread(target=sup.run_health, args=(stop,), name="health", daemon=True),
    ]

    # Built here rather than inline below because the JSON API and the event
    # stream are mounted onto this same application when they are switched on:
    # one port serves the media routes and the interfaces alike.
    app = make_live_app(buf, latest_still=lambda: sup.last_still_jpeg,
                        upstream=up, stacker=stacker,
                        latest_screenshot=lambda: sup.last_screenshot_jpeg)

    # Version first, and on its own line: "what is actually running" is the
    # first question asked when an unattended service misbehaves.
    log.info("ios2ha-camera %s, built %s", version.full(), version.BUILT)
    log.info("supervisor up: phone %s:%s, live view on :%s, interval %ss, archive %s",
             s.phone_host, s.phone_port, s.live_port, s.interval_s, s.archive_dir)
    _start_interfaces(s, sup, app, mqtt)
    for t in threads:
        t.start()
    # Started last, after the log lines above: its first tick can fire a probe
    # immediately, which must not race ahead of them in the log.
    up.start()

    # The event loop belongs to the live view alone. run_app returns on SIGTERM,
    # which is how the container stops us.
    try:
        web.run_app(app,
                    host="0.0.0.0", port=s.live_port, print=None,
                    # A /live viewer never finishes by itself, and at aiohttp's
                    # default of 60 s the container is killed before the block
                    # below ever runs.
                    shutdown_timeout=2.0)
    finally:
        log.info("shutting down")
        stop.set()
        sup.capture_requested.set()   # wake the capture loop out of its wait
        # Compose allows 30 s from SIGTERM. The event loop's shutdown above has
        # already spent up to 4 s of it, not 2: aiohttp waits shutdown_timeout
        # once for handlers to finish and again after cancelling them, and a
        # /live viewer waiting for a frame sits out both (measured 4.0 s). Open
        # event streams add nothing to that: the application's own shutdown hook
        # ends them before the first of those waits begins. Then 5 s for
        # up.stop(), 3 s for keyboard.stop(), two 3 s thread joins and up to 2 s
        # for mqtt.close() -- 20 s in all, 10 s inside the grace period. Keep
        # that sum under the compose value if a timeout here grows. Every one
        # of those is a worst case that has not been seen together; measured
        # shutdowns take 1-5 s. An upstream thread still
        # alive after its 5 s is stuck
        # in a probe, which stop() deliberately leaves alone rather than
        # touching the ingestor from a second thread. One term is outside that
        # sum: sup.close() drops every device action still queued, but the one
        # already running on the device worker is a cable or ONVIF call that
        # cannot be interrupted, and the interpreter waits for it after main()
        # returns. Normally that is well under a second. An action stuck on the
        # cable can outlast the grace period, and then the container is killed.
        up.stop(timeout=5.0)
        # Its thread is a daemon, so this is tidiness rather than necessity: it
        # unregisters the advertisement instead of leaving BlueZ to notice, and
        # ends its own poll rather than leaving asyncio to complain about it.
        keyboard.stop(timeout=3.0)
        sup.close()
        for t in threads:
            t.join(timeout=3)
        mqtt.close()                  # publishes offline; a clean disconnect has no last will


if __name__ == "__main__":
    main()
