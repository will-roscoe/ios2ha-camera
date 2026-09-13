"""MQTT client, Home Assistant discovery payloads, and the publish/subscribe surface."""

import json
import logging

import paho.mqtt.client as mqtt

from . import version

log = logging.getLogger(__name__)


def _e(prefix, comp, obj, cfg):
    topic = f"homeassistant/{comp}/ios2ha/{obj}/config"
    base = {
        "availability_topic": f"{prefix}/available",
        # sw_version puts the running version on the device page in Home
        # Assistant, so "what is deployed?" is answerable without shell access.
        "device": {"identifiers": ["ios2ha_camera"], "name": "iPhone Camera",
                   "manufacturer": "ios2ha-camera", "model": "iPhone supervisor",
                   "sw_version": version.full()},
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


# Sensor metadata. Without a unit, a device class and a state class, Home
# Assistant cannot say what a reading means, cannot graph it sensibly and keeps
# no long-term statistics for it -- "battery_temperature = 35.39" of what?
# `category="diagnostic"` keeps the thirty-odd measurements out of the main card
# while leaving them fully usable in automations and dashboards.
# Luminance figures are relative 0-255 after black-level subtraction, not lux,
# so they deliberately carry no unit; see the design's section 4.3.
_SENSORS = [
    dict(obj="stream_state", name="Stream state", device_class="enum",
         options=["streaming", "idle", "streaming_off", "unreachable"], icon="mdi:video-wireless"),
    # The USB side, which is independent of the WiFi stream above. "no_device" is
    # the cable being out, which is ordinary; "usbmuxd_down" means the daemon is
    # unreachable and the socket mount is presumed orphaned, which is a fault and
    # is what the offline alert should watch.
    dict(obj="cable_state", name="Cable state", device_class="enum",
         options=["ok", "no_device", "usbmuxd_down"], icon="mdi:usb-port",
         category="diagnostic"),
    dict(obj="subject_level", name="Subject level", state_class="measurement", precision=1,
         icon="mdi:brightness-4"),
    dict(obj="depth_used", name="Depth used", unit="frames", state_class="measurement",
         icon="mdi:layers-triple"),
    dict(obj="motion", name="Motion level", state_class="measurement", precision=2,
         icon="mdi:motion-sensor"),
    dict(obj="frame_rate", name="Frame rate", unit="fps", state_class="measurement",
         precision=1, category="diagnostic", icon="mdi:speedometer"),
    # The time of the last frame, not an age in seconds: an age is only true at
    # the instant it is published and is wrong by up to a health tick
    # afterwards, whereas Home Assistant renders a timestamp as a live relative
    # age that stays correct on its own.
    dict(obj="last_frame", name="Last frame", device_class="timestamp",
         category="diagnostic", icon="mdi:clock-outline"),
    dict(obj="depth_required", name="Depth required before clamping", unit="frames",
         state_class="measurement", category="diagnostic", icon="mdi:layers-outline"),
    dict(obj="capture_duration", name="Capture duration", unit="s", device_class="duration",
         state_class="measurement", precision=1, category="diagnostic"),
    dict(obj="colour_temperature", name="Colour temperature", unit="K",
         state_class="measurement", category="diagnostic", icon="mdi:temperature-kelvin"),
    dict(obj="effective_exposure_required", name="Effective exposure required", unit="s",
         device_class="duration", state_class="measurement", precision=2,
         category="diagnostic"),
    dict(obj="effective_exposure_achieved", name="Effective exposure achieved", unit="s",
         device_class="duration", state_class="measurement", precision=2,
         category="diagnostic"),
    dict(obj="lum_mean", name="Scene luminance mean", state_class="measurement", precision=1,
         category="diagnostic", icon="mdi:brightness-6"),
    dict(obj="dynamic_range", name="Dynamic range", state_class="measurement", precision=1,
         category="diagnostic", icon="mdi:contrast-box"),
    dict(obj="highlight_clip", name="Highlight clipping", unit="%", state_class="measurement",
         precision=2, category="diagnostic", icon="mdi:brightness-percent"),
    dict(obj="noise", name="Noise", state_class="measurement", precision=2,
         category="diagnostic", icon="mdi:grain"),
    dict(obj="snr", name="Signal to noise", state_class="measurement", precision=1,
         category="diagnostic", icon="mdi:signal"),
    dict(obj="focus_sharpness", name="Focus sharpness", state_class="measurement", precision=1,
         category="diagnostic", icon="mdi:image-filter-center-focus"),
    dict(obj="battery", name="Phone battery", unit="%", device_class="battery",
         state_class="measurement", category="diagnostic"),
    dict(obj="battery_temperature", name="Phone battery temperature", unit="°C",
         device_class="temperature", state_class="measurement", precision=1,
         category="diagnostic"),
    dict(obj="archive_free_gb", name="Archive free space", unit="GB", device_class="data_size",
         state_class="measurement", precision=1, category="diagnostic"),
    # Signed current is the one figure that says which way the battery is
    # actually going: the phone reports charging while still losing ground on
    # an underpowered port.
    dict(obj="battery_current_ma", name="Phone battery current", unit="mA",
         device_class="current", state_class="measurement", category="diagnostic"),
    dict(obj="charger_watts", name="Phone charger power", unit="W", device_class="power",
         state_class="measurement", category="diagnostic"),
    # No battery device class here: that one means state of charge, and this is
    # the cell's maximum against its design capacity.
    dict(obj="battery_health_pct", name="Phone battery health", unit="%",
         state_class="measurement", precision=1, category="diagnostic",
         icon="mdi:battery-heart-variant"),
]
def _ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


_SENSORS += [
    dict(obj=f"lum_p{p}", name=f"Scene luminance {_ordinal(p)} percentile",
         state_class="measurement", precision=1, category="diagnostic", icon="mdi:brightness-6")
    for p in (1, 10, 50, 90, 99)
]
_SENSORS += [
    dict(obj=f"wb_gain_{c}", name=f"White balance gain {c.upper()}",
         state_class="measurement", precision=3, category="diagnostic",
         icon="mdi:white-balance-auto")
    for c in "rgb"
]
_SENSORS += [
    dict(obj=f"black_level_applied_{c}", name=f"Black level applied {c.upper()}",
         state_class="measurement", precision=2, category="diagnostic", icon="mdi:circle-opacity")
    for c in "rgb"
]
_SENSORS += [
    dict(obj="holders", name="Connection holders", category="diagnostic",
         icon="mdi:account-clock"),
    dict(obj="live_fps", name="Stacked feed rate", unit="fps", state_class="measurement",
         precision=1, category="diagnostic", icon="mdi:filmstrip"),
    dict(obj="duty_1h", name="Connected in the last hour", unit="%", state_class="measurement",
         precision=1, category="diagnostic", icon="mdi:percent-outline"),
    dict(obj="tick_age", name="Upstream tick age", unit="s", device_class="duration",
         state_class="measurement", precision=1, category="diagnostic", icon="mdi:heart-pulse"),
]

# Tuning knobs. All configuration rather than measurement, and each carries its
# unit so a person reading "600" knows it is seconds, plus a device class where
# the value has a real physical type.
_NUMBERS = [
    dict(obj="interval_s", name="Capture interval", lo=30, hi=3600, step=1, unit="s",
         device_class="duration", icon="mdi:timer-outline"),
    dict(obj="fixed_depth", name="Fixed depth", lo=1, hi=1000, step=1, unit="frames",
         icon="mdi:layers-triple"),
    dict(obj="target_level", name="Target subject level", lo=1, hi=255, step=1,
         icon="mdi:brightness-4"),
    dict(obj="subject_percentile", name="Subject percentile", lo=1, hi=50, step=1, unit="%"),
    dict(obj="clean_factor", name="Clean factor k", lo=1, hi=64, step=1, icon="mdi:grain"),
    dict(obj="max_depth", name="Maximum depth", lo=1, hi=2000, step=1, unit="frames",
         icon="mdi:layers-outline"),
    dict(obj="window_max_s", name="Maximum capture window", lo=1, hi=120, step=1, unit="s",
         device_class="duration", icon="mdi:timer-sand"),
    dict(obj="ramp_tau_s", name="Ramp time constant", lo=0, hi=7200, step=10, unit="s",
         device_class="duration", icon="mdi:chart-bell-curve"),
    dict(obj="night_key", name="Night key", lo=0.01, hi=0.5, step=0.01, icon="mdi:weather-night"),
    dict(obj="stretch_strength", name="Stretch strength", lo=1, hi=60, step=1,
         icon="mdi:arrow-expand-vertical"),
    dict(obj="chroma_radius", name="Chroma smoothing radius", lo=0, hi=12, step=0.5, unit="px",
         icon="mdi:blur"),
    dict(obj="denoise_strength", name="Denoise strength", lo=0, hi=20, step=0.5,
         icon="mdi:blur-off"),
    dict(obj="black_level_0", name="Black level override R", lo=0, hi=32, step=0.1,
         icon="mdi:circle-opacity"),
    dict(obj="black_level_1", name="Black level override G", lo=0, hi=32, step=0.1,
         icon="mdi:circle-opacity"),
    dict(obj="black_level_2", name="Black level override B", lo=0, hi=32, step=0.1,
         icon="mdi:circle-opacity"),
    dict(obj="live_max_window_s", name="Stacked feed window", lo=0.5, hi=15, step=0.5, unit="s",
         device_class="duration", icon="mdi:motion-outline"),
]


def _sensor(p, spec):
    cfg = {"name": spec["name"], "state_topic": f"{p}/state/{spec['obj']}"}
    for key, ha_key in (("unit", "unit_of_measurement"), ("device_class", "device_class"),
                        ("state_class", "state_class"), ("options", "options"),
                        ("precision", "suggested_display_precision"),
                        ("category", "entity_category"), ("icon", "icon")):
        if key in spec:
            cfg[ha_key] = spec[key]
    return cfg


def discovery_payloads(s):
    p = s.mqtt_prefix

    def st(x):
        return f"{p}/state/{x}"

    def cmd(x):
        return f"{p}/set/{x}"

    out = []
    # name null marks the still as the device's main feature, so it shows as
    # "iPhone Camera" rather than "iPhone Camera Camera".
    out.append(_e(p, "camera", "still", {"name": None, "topic": f"{p}/still",
                                         "icon": "mdi:camera"}))
    # A retained PNG is published here when the screenshot button is pressed;
    # a distinct entity so it doesn't collide with the still camera above.
    out.append(_e(p, "camera", "screenshot", {"name": "Screenshot", "topic": f"{p}/screenshot",
                                              "icon": "mdi:cellphone-screenshot",
                                              "entity_category": "diagnostic"}))
    for spec in _NUMBERS:
        name = spec["obj"]
        cfg = {"name": spec["name"], "state_topic": st(name), "command_topic": cmd(name),
               "min": spec["lo"], "max": spec["hi"], "step": spec["step"],
               "entity_category": "config", "mode": "box"}
        if "unit" in spec:
            cfg["unit_of_measurement"] = spec["unit"]
        for key in ("device_class", "icon"):
            if key in spec:
                cfg[key] = spec[key]
        out.append(_e(p, "number", name, cfg))
    selects = {"stacking_mode": (["off", "fixed", "auto"], "Stacking mode"),
               "white_balance": (["as-shot", "grey-world", "brightest-patch"], "White balance"),
               "render_intent": (["natural", "inspection"], "Render intent")}
    for name, (opts, label) in selects.items():
        out.append(_e(p, "select", name, {"name": label, "state_topic": st(name),
                     "command_topic": cmd(name), "options": opts,
                     "entity_category": "config"}))
    for name, label, icon in (("stacking_allowed", "Stacking allowed", "mdi:layers"),
                              ("autofocus", "Autofocus", "mdi:image-filter-center-focus-strong"),
                              ("on_demand", "On demand", "mdi:power-plug-off-outline")):
        out.append(_e(p, "switch", name, {"name": label, "state_topic": st(name),
                     "command_topic": cmd(name), "payload_on": "true", "payload_off": "false",
                     "entity_category": "config", "icon": icon}))
    # capture_now stays uncategorised: it is the one button that belongs on a
    # dashboard rather than buried under configuration or diagnostics.
    buttons = [
        dict(obj="capture_now", name="Capture now", icon="mdi:camera-iris"),
        dict(obj="screenshot", name="Phone screenshot", icon="mdi:cellphone-screenshot",
             category="diagnostic"),
        dict(obj="relaunch_app", name="Relaunch camera app", device_class="restart",
             category="config"),
        dict(obj="prepare_phone", name="Prepare phone", icon="mdi:cellphone-cog",
             category="config"),
    ]
    for spec in buttons:
        cfg = {"name": spec["name"], "command_topic": cmd(spec["obj"])}
        if "icon" in spec:
            cfg["icon"] = spec["icon"]
        if "device_class" in spec:
            cfg["device_class"] = spec["device_class"]
        if "category" in spec:
            cfg["entity_category"] = spec["category"]
        out.append(_e(p, "button", spec["obj"], cfg))
    for spec in _SENSORS:
        out.append(_e(p, "sensor", spec["obj"], _sensor(p, spec)))
    for name, label, device_class in (("camera_online", "Camera online", "connectivity"),
                                      ("charging", "Phone charging", "battery_charging")):
        out.append(_e(p, "binary_sensor", name, {"name": label, "state_topic": st(name),
                     "payload_on": "true", "payload_off": "false",
                     "device_class": device_class, "entity_category": "diagnostic"}))
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
            # Published as built, with no None-stripping: `"name": null` is a
            # meaningful value that marks an entity as its device's main
            # feature, and is not the same as leaving the key out. The builders
            # omit keys that do not apply.
            c.publish(topic, json.dumps(cfg), retain=True)
        self.set_available(True)
        # Runs on every (re)connect, so the supervisor can republish state that a
        # broker restart dropped.
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
