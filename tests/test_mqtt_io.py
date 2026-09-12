from ios2ha_camera.mqtt_io import discovery_payloads
from ios2ha_camera.settings import Settings, CONTROLS


def test_discovery_covers_camera_and_controls():
    payloads = dict(discovery_payloads(Settings()))
    topics = " ".join(payloads)
    assert "/camera/" in topics
    # every writable control has a discovery entity of some component type
    for c in ("interval_s", "render_intent", "white_balance", "autofocus", "stacking_mode"):
        assert any(c in cfg.get("command_topic", "") for cfg in payloads.values())


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
