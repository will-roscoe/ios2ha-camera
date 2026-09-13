from ios2ha_camera.mqtt_io import MqttIO, discovery_payloads
from ios2ha_camera.settings import CONTROLS, Settings


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


def test_close_is_quiet_when_never_connected():
    """Shutting down with the broker unreachable is ordinary; it must not raise
    and must not put a traceback in the log."""
    io = MqttIO(Settings())
    io.close(timeout=0.1)


def test_a_non_utf8_payload_never_escapes_the_message_callback():
    """paho re-raises callback exceptions by default, so one corrupt retained
    message on set/# would take the whole connection down."""
    io = MqttIO(Settings())
    io._on_message(None, None, _Msg("ios2ha_camera/set/interval_s", b"\xff\xfe\x00bad"))


def test_a_raising_control_handler_never_escapes_the_message_callback():
    def boom(name, value):
        raise RuntimeError("the camera is on fire")

    io = MqttIO(Settings(), on_control=boom)
    io._on_message(None, None, _Msg("ios2ha_camera/set/interval_s", b"300"))


def test_control_payloads_decode_to_the_right_python_types():
    seen = []
    io = MqttIO(Settings(), on_control=lambda n, v: seen.append((n, v)))
    for topic, payload in [("set/interval_s", b"300"), ("set/autofocus", b"true"),
                           ("set/render_intent", b"natural"), ("set/night_key", b"0.09")]:
        io._on_message(None, None, _Msg(f"ios2ha_camera/{topic}", payload))
    assert seen == [("interval_s", 300), ("autofocus", True),
                    ("render_intent", "natural"), ("night_key", 0.09)]


def test_discovery_covers_camera_and_controls():
    payloads = dict(discovery_payloads(Settings()))
    topics = " ".join(payloads)
    assert "/camera/" in topics
    # every writable control has a discovery entity of some component type
    for c in ("interval_s", "render_intent", "white_balance", "autofocus", "stacking_mode"):
        assert any(c in cfg.get("command_topic", "") for cfg in payloads.values())


def test_every_sensor_says_what_its_reading_means():
    """A bare number is not a measurement. Without a unit, a device class and a
    state class, Home Assistant cannot graph it or keep statistics for it."""
    payloads = dict(discovery_payloads(Settings()))
    sensors = {t.split("/")[3]: cfg for t, cfg in payloads.items()
               if t.startswith("homeassistant/sensor/")}
    assert sensors, "no sensors found"
    # Luminance figures are relative 0-255 after black level, not lux, so they
    # carry no unit by design; everything else that is a quantity needs one.
    # last_frame is a timestamp: no unit, and a timestamp sensor must not carry
    # a state_class either.
    # holders is a list of who is holding the connection open (or "none"), not
    # a quantity, so it carries neither a unit nor a state_class either.
    not_a_measurement = {"stream_state", "last_frame", "holders"}
    unitless_by_design = {"subject_level", "lum_mean", "dynamic_range", "noise", "snr",
                          "focus_sharpness", "motion"} | not_a_measurement
    unitless_by_design |= {f"lum_p{p}" for p in (1, 10, 50, 90, 99)}
    unitless_by_design |= {f"wb_gain_{c}" for c in "rgb"}
    unitless_by_design |= {f"black_level_applied_{c}" for c in "rgb"}
    for obj, cfg in sensors.items():
        assert cfg["name"] != obj, f"{obj} still has a raw snake_case name"
        if obj not in unitless_by_design:
            assert cfg.get("unit_of_measurement"), f"{obj} has no unit"
        if obj not in not_a_measurement:
            assert cfg.get("state_class") == "measurement", f"{obj} has no state_class"


def test_the_readings_with_a_real_physical_type_declare_it():
    payloads = dict(discovery_payloads(Settings()))
    sensors = {t.split("/")[3]: cfg for t, cfg in payloads.items()
               if t.startswith("homeassistant/sensor/")}
    assert sensors["battery_temperature"]["device_class"] == "temperature"
    assert sensors["battery_temperature"]["unit_of_measurement"] == "°C"
    assert sensors["battery"]["device_class"] == "battery"
    assert sensors["battery"]["unit_of_measurement"] == "%"
    assert sensors["archive_free_gb"]["device_class"] == "data_size"
    assert sensors["last_frame"]["device_class"] == "timestamp"
    assert "state_class" not in sensors["last_frame"], "a timestamp cannot be a measurement"
    assert "last_frame_age" not in sensors, "replaced by the timestamp"
    assert sensors["stream_state"]["device_class"] == "enum"
    assert set(sensors["stream_state"]["options"]) == {"streaming", "idle", "streaming_off", "unreachable"}
    binaries = {t.split("/")[3]: cfg for t, cfg in payloads.items()
                if t.startswith("homeassistant/binary_sensor/")}
    assert binaries["camera_online"]["device_class"] == "connectivity"
    assert binaries["charging"]["device_class"] == "battery_charging"


def test_the_still_camera_is_the_devices_main_feature():
    """A null name inherits the device name, so it reads "iPhone Camera"
    rather than "iPhone Camera Camera"."""
    payloads = dict(discovery_payloads(Settings()))
    still = [cfg for t, cfg in payloads.items() if t.endswith("/camera/ios2ha/still/config")][0]
    assert "name" in still and still["name"] is None


def test_tuning_knobs_are_marked_as_configuration():
    payloads = dict(discovery_payloads(Settings()))
    for topic, cfg in payloads.items():
        component = topic.split("/")[1]
        if component in ("number", "select", "switch"):
            assert cfg.get("entity_category") == "config", topic


def test_every_entity_of_every_type_has_a_readable_name():
    """Applies to controls as much as sensors: a raw snake_case object id is
    not a name anyone should have to read in a dashboard."""
    payloads = dict(discovery_payloads(Settings()))
    for topic, cfg in payloads.items():
        obj = topic.split("/")[3]
        assert "name" in cfg, topic
        if cfg["name"] is None:
            # Only the main feature may inherit the device name.
            assert topic.endswith("/camera/ios2ha/still/config"), topic
            continue
        assert cfg["name"] != obj, f"{topic} has a raw object id as its name"
        assert cfg["name"][0].isupper(), f"{topic} name is not sentence case: {cfg['name']!r}"


def test_controls_declare_their_units_and_physical_types():
    payloads = dict(discovery_payloads(Settings()))
    numbers = {t.split("/")[3]: cfg for t, cfg in payloads.items()
               if t.startswith("homeassistant/number/")}
    # Anything measured in seconds says so, and says it is a duration.
    for obj in ("interval_s", "window_max_s", "ramp_tau_s"):
        assert numbers[obj]["unit_of_measurement"] == "s", obj
        assert numbers[obj]["device_class"] == "duration", obj
    assert numbers["subject_percentile"]["unit_of_measurement"] == "%"
    assert numbers["fixed_depth"]["unit_of_measurement"] == "frames"
    for obj, cfg in numbers.items():
        assert {"min", "max", "step"} <= set(cfg), f"{obj} has no range"
        assert cfg["min"] < cfg["max"], obj
    buttons = {t.split("/")[3]: cfg for t, cfg in payloads.items()
               if t.startswith("homeassistant/button/")}
    assert buttons["relaunch_app"]["device_class"] == "restart"
    # The one button that belongs on a dashboard is not filed away.
    assert "entity_category" not in buttons["capture_now"]


def test_entity_ids_are_pinned_not_derived_from_display_names():
    """Automations and dashboards name these entities. Left to Home Assistant,
    the id comes from the device and entity display names, so renaming either
    would break every reference."""
    payloads = dict(discovery_payloads(Settings()))
    ids = {cfg["default_entity_id"] for cfg in payloads.values()}
    assert "camera.ios2ha_camera_still" in ids
    assert "binary_sensor.ios2ha_camera_camera_online" in ids
    assert "sensor.ios2ha_camera_stream_state" in ids
    for topic, cfg in payloads.items():
        component = topic.split("/")[1]
        assert cfg["default_entity_id"].startswith(component + ".")


def test_unique_ids_do_not_collide_across_domains():
    payloads = dict(discovery_payloads(Settings()))
    uids = [cfg["unique_id"] for cfg in payloads.values()]
    assert len(uids) == len(set(uids)), "screenshot exists as both a button and a camera"


def test_discovery_sets_availability_topic():
    for _topic, cfg in discovery_payloads(Settings()):
        assert cfg.get("availability_topic", "").endswith("/available")


def test_every_control_is_writable_or_documented():
    payloads = dict(discovery_payloads(Settings()))
    cmd_controls = set()
    for cfg in payloads.values():
        ct = cfg.get("command_topic", "")
        if ct:
            cmd_controls.add(ct.rsplit("/", 1)[-1])
    # black_level_override is exposed as three numbers (black_level_0/1/2), whose
    # names are shorter than "black_level_override" itself, so a plain k.startswith(c)
    # can never match; compare stems (the name minus its final _suffix) as well.
    missing = {c for c in CONTROLS if c not in cmd_controls
               and not any(k.startswith(c) for k in cmd_controls)
               and not any(k.startswith(c.rsplit("_", 1)[0]) for k in cmd_controls)}
    assert missing == set(), f"controls with no command entity: {missing}"


def test_still_camera_entity_has_only_name_and_topic():
    """The supervisor publishes raw JPEG bytes; HA's MQTT camera default is raw,
    so no image_encoding key should be present."""
    s = Settings()
    payloads = dict(discovery_payloads(s))
    still = [cfg for topic, cfg in payloads.items() if topic.endswith("/camera/ios2ha/still/config")]
    assert len(still) == 1
    cfg = still[0]
    assert cfg["topic"] == f"{s.mqtt_prefix}/still"
    assert "image_encoding" not in cfg


def test_screenshot_camera_entity_present():
    """The screenshot button publishes a retained PNG to {prefix}/screenshot;
    without this entity that payload has no consumer."""
    s = Settings()
    payloads = dict(discovery_payloads(s))
    shot = [cfg for topic, cfg in payloads.items() if topic.endswith("/camera/ios2ha/screenshot/config")]
    assert len(shot) == 1
    assert shot[0]["topic"] == f"{s.mqtt_prefix}/screenshot"


def test_extra_measurement_sensors_present():
    payloads = dict(discovery_payloads(Settings()))
    sensor_objs = {topic.split("/")[3] for topic in payloads if topic.startswith("homeassistant/sensor/")}
    for name in ("lum_mean", "snr", "wb_gain_r", "wb_gain_g", "wb_gain_b",
                 "black_level_applied_r", "black_level_applied_g", "black_level_applied_b"):
        assert name in sensor_objs


def test_charging_binary_sensor_present():
    payloads = dict(discovery_payloads(Settings()))
    binary_objs = {topic.split("/")[3] for topic in payloads if topic.startswith("homeassistant/binary_sensor/")}
    assert {"camera_online", "charging"} <= binary_objs
    cfg = dict(discovery_payloads(Settings()))[
        [t for t in payloads if t.endswith("/binary_sensor/ios2ha/charging/config")][0]
    ]
    assert cfg["payload_on"] == "true" and cfg["payload_off"] == "false"


def test_the_new_entities_are_discovered_with_pinned_ids():
    ids = {cfg["default_entity_id"] for _, cfg in discovery_payloads(Settings())}
    assert {"switch.ios2ha_camera_on_demand",
            "number.ios2ha_camera_live_max_window_s",
            "button.ios2ha_camera_prepare_phone",
            "sensor.ios2ha_camera_holders",
            "sensor.ios2ha_camera_live_fps",
            "sensor.ios2ha_camera_duty_1h",
            "sensor.ios2ha_camera_tick_age"} <= ids


def test_stream_state_can_report_idle():
    cfg = next(c for _, c in discovery_payloads(Settings())
               if c["default_entity_id"] == "sensor.ios2ha_camera_stream_state")
    assert "idle" in cfg["options"]
