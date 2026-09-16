"""MQTT client, Home Assistant discovery payloads, and the publish/subscribe surface."""

import json
import logging
import threading
import time

import paho.mqtt.client as mqtt

from .entities import ENTITIES, device_info
from .settings import mqtt_endpoints

log = logging.getLogger(__name__)


def _e(prefix, comp, obj, cfg):
    topic = f"homeassistant/{comp}/ios2ha/{obj}/config"
    base = {
        "availability_topic": f"{prefix}/available",
        # Declared in entities.device_info(), not here: the API serves the same
        # block from /api/v1/info and the two must describe one device.
        "device": device_info(),
        # Component in the unique_id because a button and a camera can share an
        # object name (screenshot) and only differ by domain.
        "unique_id": f"ios2ha_{comp}_{obj}",
        # Pin the entity_id. Home Assistant otherwise derives it from the device
        # and entity display names -- "iPhone Camera" + "Camera" would give
        # camera.iphone_camera_camera -- so a rename would silently break every
        # automation and dashboard pointing at it. The object_id in the
        # discovery topic explicitly does not affect entity_id; this key does.
        "default_entity_id": f"{comp}.ios2ha_camera_{obj}",
    }
    base.update(cfg)
    return topic, base


# One renderer per domain, each turning an `entities.Entity` into the discovery
# config Home Assistant expects. They only ever *read* the record: what an entity
# is lives in entities.py, and this module decides how Home Assistant is told
# about it. A domain with no renderer here raises rather than being skipped, so a
# new kind of entity cannot be declared and then silently never published.


def _camera_cfg(p, e):
    cfg = {"name": e.name, "topic": f"{p}/{e.topic}"}
    if e.icon:
        cfg["icon"] = e.icon
    if e.category:
        cfg["entity_category"] = e.category
    return cfg


def _number_cfg(p, e):
    cfg = {"name": e.name, "state_topic": f"{p}/state/{e.object_id}",
           "command_topic": f"{p}/set/{e.object_id}",
           "min": e.lo, "max": e.hi, "step": e.step,
           "entity_category": e.category, "mode": "box"}
    if e.unit is not None:
        cfg["unit_of_measurement"] = e.unit
    if e.device_class:
        cfg["device_class"] = e.device_class
    if e.icon:
        cfg["icon"] = e.icon
    return cfg


def _select_cfg(p, e):
    return {"name": e.name, "state_topic": f"{p}/state/{e.object_id}",
            "command_topic": f"{p}/set/{e.object_id}", "options": list(e.options),
            "entity_category": e.category}


def _switch_cfg(p, e):
    return {"name": e.name, "state_topic": f"{p}/state/{e.object_id}",
            "command_topic": f"{p}/set/{e.object_id}",
            "payload_on": "true", "payload_off": "false",
            "entity_category": e.category, "icon": e.icon}


def _button_cfg(p, e):
    cfg = {"name": e.name, "command_topic": f"{p}/set/{e.object_id}"}
    if e.icon:
        cfg["icon"] = e.icon
    if e.device_class:
        cfg["device_class"] = e.device_class
    if e.category:
        cfg["entity_category"] = e.category
    return cfg


def _sensor_cfg(p, e):
    cfg = {"name": e.name, "state_topic": f"{p}/state/{e.object_id}"}
    for value, ha_key in ((e.unit, "unit_of_measurement"),
                          (e.device_class, "device_class"),
                          (e.state_class, "state_class"),
                          (list(e.options) if e.options else None, "options"),
                          (e.precision, "suggested_display_precision"),
                          (e.category, "entity_category"),
                          (e.icon, "icon")):
        if value is not None:
            cfg[ha_key] = value
    return cfg


def _binary_sensor_cfg(p, e):
    cfg = {"name": e.name, "state_topic": f"{p}/state/{e.object_id}",
           "payload_on": "true", "payload_off": "false",
           "entity_category": e.category}
    if e.device_class:
        cfg["device_class"] = e.device_class
    return cfg


_RENDERERS = {
    "camera": _camera_cfg,
    "number": _number_cfg,
    "select": _select_cfg,
    "switch": _switch_cfg,
    "button": _button_cfg,
    "sensor": _sensor_cfg,
    "binary_sensor": _binary_sensor_cfg,
}


def discovery_payloads(s):
    """Every entity in `entities.ENTITIES`, rendered as a Home Assistant discovery
    payload, in declaration order."""
    p = s.mqtt_prefix
    out = []
    for e in ENTITIES:
        render = _RENDERERS.get(e.domain)
        if render is None:
            raise ValueError(f"{e.object_id}: no discovery renderer for domain {e.domain!r}")
        out.append(_e(p, e.domain, e.object_id, render(p, e)))
    return out


class MqttIO:
    def __init__(self, settings, on_control=lambda n, v: None, on_connected=lambda: None):
        self.s = settings
        self.on_control = on_control
        self.on_connected = on_connected
        # Resolved once, here: a malformed MQTT_URL fails at startup with a message
        # naming the problem, rather than as a connection that never comes up.
        self._endpoints = mqtt_endpoints(settings)
        self._endpoint = 0
        first = self._endpoints[0]
        # Transport and TLS are fixed per client, which is why a fallback list must
        # share one scheme (parse_mqtt_urls enforces it).
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport=first.transport)
        if first.tls:
            self.c.tls_set()
        if first.transport == "websockets":
            self.c.ws_set_options(path=first.path)
        if settings.mqtt_user:
            self.c.username_pw_set(settings.mqtt_user, settings.mqtt_password)
        self.c.will_set(f"{settings.mqtt_prefix}/available", "offline", retain=True)
        self.c.on_connect = self._on_connect
        self.c.on_message = self._on_message
        self.c.on_connect_fail = self._on_connect_fail

    def _on_connect(self, c, u, flags, rc, props=None):
        # Guarded for the same reason as _on_message: this also runs on paho's
        # network thread, which re-raises a callback exception and takes the
        # connection down with it. discovery_payloads raises on purpose for an
        # entity it cannot render, and that must cost the discovery, not every
        # later control.
        try:
            c.subscribe(f"{self.s.mqtt_prefix}/set/#")
            for topic, cfg in discovery_payloads(self.s):
                # Published as built, with no None-stripping: `"name": null` is
                # a meaningful value that marks an entity as its device's main
                # feature, and is not the same as leaving the key out. The
                # builders omit keys that do not apply.
                c.publish(topic, json.dumps(cfg), retain=True)
            self.set_available(True)
        except Exception:
            log.exception("MQTT connect handling failed")
        # Runs on every (re)connect, so the supervisor can republish state that a
        # broker restart dropped. Separately guarded, so a discovery failure
        # above does not also cost the replay.
        try:
            self.on_connected()
        except Exception:
            log.exception("on_connected callback failed")

    def _on_message(self, c, u, msg):
        # Everything, including the decode, is inside the guard: paho re-raises
        # callback exceptions out of the network loop by default
        # (suppress_exceptions is False), so one non-UTF-8 payload on set/#
        # would take the connection down and leave the supervisor deaf to every
        # later control.
        try:
            name = msg.topic.rsplit("/", 1)[-1]
            raw = msg.payload.decode(errors="replace")
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw
            self.on_control(name, value)
        except Exception:
            log.exception("control message on %s failed", msg.topic)

    def connect(self):
        # Asynchronous so a broker that is down at startup does not abort the
        # process; paho retries in the background once the loop is running,
        # while capture and archiving carry on.
        ep = self._endpoints[self._endpoint]
        self.c.connect_async(ep.host, ep.port, keepalive=30)

    def _on_connect_fail(self, client, userdata):
        """paho calls this after each failed attempt, before it retries. With more
        than one broker in MQTT_URL, aim the retry at the next one, wrapping round."""
        if len(self._endpoints) < 2:
            return
        self._endpoint = (self._endpoint + 1) % len(self._endpoints)
        ep = self._endpoints[self._endpoint]
        # host and port may only be changed while disconnected, which is exactly
        # when this runs; reconnect() then reads them back.
        client.host, client.port = ep.host, ep.port
        if ep.transport == "websockets":
            client.ws_set_options(path=ep.path)
        log.info("MQTT broker unreachable; trying %s://%s:%s next", ep.scheme, ep.host, ep.port)

    def loop_start(self):
        self.c.loop_start()

    def loop_stop(self):
        self.c.loop_stop()

    def publish_still(self, jpeg):
        self.c.publish(f"{self.s.mqtt_prefix}/still", jpeg, retain=True)

    def publish_screenshot(self, png):
        self.c.publish(f"{self.s.mqtt_prefix}/screenshot", png, retain=True)

    def publish_state(self, d, retain=True):
        # Retained by default so Home Assistant shows real values immediately
        # after its own restart rather than "unknown" until the next interval.
        for k, v in d.items():
            self.c.publish(f"{self.s.mqtt_prefix}/state/{k}",
                           v if isinstance(v, (str, bytes)) else json.dumps(v), retain=retain)

    def publish_event(self, d):
        self.c.publish(f"{self.s.mqtt_prefix}/event", json.dumps(d))

    def set_available(self, ok):
        self.c.publish(f"{self.s.mqtt_prefix}/available",
                       "online" if ok else "offline", retain=True)

    def close(self, timeout=2.0):
        """Go offline deliberately, then disconnect.

        A clean disconnect does not fire the last will, so availability has to
        be published here, at QoS 1 and flushed, or Home Assistant would keep
        showing the camera as online after a planned shutdown.
        """
        try:
            info = self.c.publish(f"{self.s.mqtt_prefix}/available", "offline", qos=1, retain=True)
            info.wait_for_publish(timeout)
        except (RuntimeError, ValueError) as e:
            # Being disconnected at shutdown is ordinary: the broker may be down,
            # or we may never have reached it. The last will covers that case, so
            # this is a note rather than a fault, and does not deserve a
            # traceback in the log of an unattended service.
            log.info("offline availability not published: %s", e)
        except Exception:
            log.warning("could not publish offline availability", exc_info=True)
        self.c.disconnect()
        self.c.loop_stop()

    def teardown_discovery(self, timeout=5.0):
        """Delete every MQTT-discovered entity: publish an empty retained payload
        to each discovery topic, which is how Home Assistant's MQTT integration
        forgets an entity -- without it the registry keeps ghosts and their
        entity ids stay reserved. Called once from `__main__` startup when
        EXPOSE_MQTT is false, instead of running the supervisor's normal MQTT
        lifecycle (`connect()` / `loop_start()`).

        A native-only deployment may have no broker at all, so the whole call
        is bounded by `timeout` seconds -- one connection attempt, plus
        whatever is left of the budget to publish -- and gives up with a
        single log line, never the endless retry EXPOSE_MQTT exists to stop.
        Topics come from discovery_payloads(self.s), the same registry
        _on_connect publishes from, so nothing cleared here can drift from what
        would have been created. The retained `<prefix>/available` message is
        cleared as well, after them and within the same deadline.
        """
        if timeout <= 0:
            # paho's own connect_timeout setter raises ValueError for this; catch
            # it here so a bad call is one log line, not a crash during startup.
            log.warning("MQTT teardown skipped: timeout must be positive, got %r", timeout)
            return
        discovery = [topic for topic, _cfg in discovery_payloads(self.s)]
        # The retained availability message goes too, and last: left behind, a
        # broker keeps announcing a device that has no entities, and cleared
        # before them, a teardown cut short would leave entities whose
        # availability topic is already gone.
        topics = discovery + [f"{self.s.mqtt_prefix}/available"]
        ep = self._endpoints[self._endpoint]
        connected = threading.Event()
        original_on_connect = self.c.on_connect

        def _on_connect(c, u, flags, rc, props=None):
            if rc == 0:
                connected.set()

        self.c.on_connect = _on_connect
        self.c.connect_timeout = timeout
        deadline = time.monotonic() + timeout
        try:
            try:
                # keepalive must cover the whole teardown budget: the default
                # paho ships (60s) or a short hardcoded value could otherwise
                # let the broker drop the connection partway through a slow
                # run of the (currently 86-entry) discovery-topic loop, before
                # the deadline below ever gets a chance to end things cleanly.
                self.c.connect(ep.host, ep.port, keepalive=max(30, int(timeout) + 5))
            except OSError as e:
                # Refused, unresolvable or timed out at the socket level -- all
                # ordinary for a deployment with no broker, not a fault.
                log.warning("MQTT teardown skipped: %s:%s unreachable (%s)", ep.host, ep.port, e)
                return
            # connect() only opens the socket and sends CONNECT; the CONNACK that
            # confirms it -- what _on_connect above is waiting for -- is only
            # processed once the network loop is running (see paho's own
            # docstring on connect()). Restoring on_connect before this point
            # would hand that CONNACK to the real discovery-publishing handler
            # instead, and connected.wait() below would time out even on a
            # perfectly reachable broker.
            self.c.loop_start()
            if not connected.wait(max(deadline - time.monotonic(), 0)):
                log.warning("MQTT teardown skipped: no response from %s:%s within %.0fs",
                            ep.host, ep.port, timeout)
                return
            # One deadline for the whole loop, not one timeout per topic: with
            # 86 discovery topics, a per-topic wait would let a slow or dying
            # connection stretch teardown to 86 * timeout, which is exactly
            # the kind of unbounded wait EXPOSE_MQTT exists to avoid.
            cleared = 0
            gave_up = None
            for topic in topics:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    gave_up = "ran out of time"
                    break
                try:
                    info = self.c.publish(topic, "", retain=True)
                    info.wait_for_publish(remaining)
                except (RuntimeError, ValueError) as e:
                    # The broker dropped the connection mid-teardown (or this
                    # client's own keepalive expired): every publish after
                    # this one would fail the same way, so stop rather than
                    # repeat the failure for each remaining topic.
                    gave_up = f"connection lost ({e})"
                    break
                cleared += 1
            if gave_up:
                log.warning("MQTT teardown stopped after clearing %d of %d retained topics: %s",
                            cleared, len(topics), gave_up)
            else:
                log.info("MQTT discovery cleared: %d entities removed and availability "
                         "retracted (EXPOSE_MQTT=false)", len(discovery))
        finally:
            self.c.on_connect = original_on_connect
            # Matches close(), a few lines up: disconnect (which sends the
            # DISCONNECT packet over the still-running network loop) before
            # loop_stop (which joins that loop's thread) -- the reverse order
            # would join the thread before it can send the packet.
            self.c.disconnect()
            self.c.loop_stop()


class NullMqttIO:
    """Stands in for MqttIO when EXPOSE_MQTT is false: the same calls, no broker.

    The supervisor publishes its state, its events, its stills and its
    availability without knowing whether anyone is listening, and guarding each
    of those call sites with the flag would scatter the question "is MQTT on?"
    across a module that does not otherwise care. It is answered once instead,
    by which of these two objects startup hands over.

    Nothing is queued for a connection that might come later: with discovery off
    there is no consumer for any of it, and the supervisor remembers its own
    values either way, so the JSON API still answers with real ones.

    `teardown_discovery` is deliberately absent. Clearing the retained discovery
    topics needs a real broker, and a copy of it here that quietly did nothing
    would report success for entities that were never removed.
    """

    def connect(self):
        pass

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def publish_still(self, jpeg):
        pass

    def publish_screenshot(self, png):
        pass

    def publish_state(self, d, retain=True):
        pass

    def publish_event(self, d):
        pass

    def set_available(self, ok):
        pass

    def close(self, timeout=2.0):
        pass
