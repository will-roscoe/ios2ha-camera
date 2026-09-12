"""MQTT client, Home Assistant discovery payloads, and the publish/subscribe surface."""

import json
import logging

import paho.mqtt.client as mqtt

log = logging.getLogger(__name__)


def _e(prefix, comp, obj, cfg):
    topic = f"homeassistant/{comp}/ios2ha/{obj}/config"
    base = {
        "availability_topic": f"{prefix}/available",
        "device": {"identifiers": ["ios2ha_camera"], "name": "iPhone Camera"},
        "unique_id": f"ios2ha_{obj}",
    }
    base.update(cfg)
    return topic, base


def discovery_payloads(s):
    p = s.mqtt_prefix

    def st(x):
        return f"{p}/state/{x}"

    def cmd(x):
        return f"{p}/set/{x}"

    out = []
    out.append(_e(p, "camera", "still", {"name": "Camera", "topic": f"{p}/still"}))
    # HA publishes a retained PNG here when the screenshot button is pressed;
    # a distinct entity so it doesn't collide with the still camera above.
    out.append(_e(p, "camera", "screenshot", {"name": "Screenshot", "topic": f"{p}/screenshot"}))
    numbers = {
        "interval_s": (30, 3600, 1), "fixed_depth": (1, 1000, 1),
        "target_level": (1, 255, 1), "subject_percentile": (1, 50, 1),
        "clean_factor": (1, 64, 1), "max_depth": (1, 2000, 1),
        "window_max_s": (1, 120, 1), "ramp_tau_s": (0, 7200, 10),
        "night_key": (0.01, 0.5, 0.01), "stretch_strength": (1, 60, 1),
        "chroma_radius": (0, 12, 0.5), "denoise_strength": (0, 20, 0.5),
    }
    for name, (lo, hi, step) in numbers.items():
        out.append(_e(p, "number", name, {"name": name, "state_topic": st(name),
                     "command_topic": cmd(name), "min": lo, "max": hi, "step": step}))
    for c in range(3):
        out.append(_e(p, "number", f"black_level_{c}", {"name": f"black level {c}",
                     "state_topic": st(f"black_level_{c}"), "command_topic": cmd(f"black_level_{c}"),
                     "min": 0, "max": 32, "step": 0.1}))
    selects = {"stacking_mode": ["off", "fixed", "auto"],
               "white_balance": ["as-shot", "grey-world", "brightest-patch"],
               "render_intent": ["natural", "inspection"]}
    for name, opts in selects.items():
        out.append(_e(p, "select", name, {"name": name, "state_topic": st(name),
                     "command_topic": cmd(name), "options": opts}))
    for name in ("stacking_allowed", "autofocus"):
        out.append(_e(p, "switch", name, {"name": name, "state_topic": st(name),
                     "command_topic": cmd(name), "payload_on": "true", "payload_off": "false"}))
    for name in ("capture_now", "screenshot", "relaunch_app"):
        out.append(_e(p, "button", name, {"name": name, "command_topic": cmd(name)}))
    sensors = ["stream_state", "frame_rate", "last_frame_age", "subject_level",
               "depth_used", "depth_required", "capture_duration", "colour_temperature",
               "effective_exposure_required", "effective_exposure_achieved",
               "lum_mean", "lum_p1", "lum_p10", "lum_p50", "lum_p90", "lum_p99", "dynamic_range",
               "highlight_clip", "noise", "snr", "motion", "focus_sharpness",
               "wb_gain_r", "wb_gain_g", "wb_gain_b",
               "black_level_applied_r", "black_level_applied_g", "black_level_applied_b",
               "battery", "battery_temperature", "archive_free_gb"]
    for name in sensors:
        out.append(_e(p, "sensor", name, {"name": name, "state_topic": st(name)}))
    for name, label in (("camera_online", "camera online"), ("charging", "charging")):
        out.append(_e(p, "binary_sensor", name, {"name": label,
                     "state_topic": st(name), "payload_on": "true", "payload_off": "false"}))
    return out


class MqttIO:
    def __init__(self, settings, on_control=lambda n, v: None, on_connected=lambda: None):
        self.s = settings
        self.on_control = on_control
        self.on_connected = on_connected
        self.c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if settings.mqtt_user:
            self.c.username_pw_set(settings.mqtt_user, settings.mqtt_password)
        self.c.will_set(f"{settings.mqtt_prefix}/available", "offline", retain=True)
        self.c.on_connect = self._on_connect
        self.c.on_message = self._on_message

    def _on_connect(self, c, u, flags, rc, props=None):
        c.subscribe(f"{self.s.mqtt_prefix}/set/#")
        for topic, cfg in discovery_payloads(self.s):
            c.publish(topic, json.dumps({k: v for k, v in cfg.items() if v is not None}), retain=True)
        self.set_available(True)
        # Runs on every (re)connect, so the supervisor can republish state that a
        # broker restart dropped.
        try:
            self.on_connected()
        except Exception:
            log.exception("on_connected callback failed")

    def _on_message(self, c, u, msg):
        name = msg.topic.rsplit("/", 1)[-1]
        raw = msg.payload.decode()
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
        # paho re-raises callback exceptions out of the network loop by default
        # (suppress_exceptions is False), which would take the connection down
        # and leave the supervisor deaf to every later control.
        try:
            self.on_control(name, value)
        except Exception:
            log.exception("control %r from %s failed", name, msg.topic)

    def connect(self):
        # Asynchronous so a broker that is down at startup does not abort the
        # process; paho retries in the background once the loop is running,
        # while capture and archiving carry on.
        self.c.connect_async(self.s.mqtt_host, self.s.mqtt_port, keepalive=30)

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
        self.c.publish(f"{self.s.mqtt_prefix}/available", "online" if ok else "offline", retain=True)

    def close(self, timeout=2.0):
        """Go offline deliberately, then disconnect.

        A clean disconnect does not fire the last will, so availability has to
        be published here, at QoS 1 and flushed, or Home Assistant would keep
        showing the camera as online after a planned shutdown.
        """
        try:
            info = self.c.publish(f"{self.s.mqtt_prefix}/available", "offline", qos=1, retain=True)
            info.wait_for_publish(timeout)
        except Exception:
            log.warning("could not publish offline availability", exc_info=True)
        self.c.disconnect()
        self.c.loop_stop()
