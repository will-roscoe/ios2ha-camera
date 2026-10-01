import inspect
import logging

from ios2ha_camera import mqtt_io as mqtt_io_module
from ios2ha_camera.mqtt_io import MqttIO, NullMqttIO, discovery_payloads
from ios2ha_camera.settings import CONTROLS, Settings
from ios2ha_camera.states import STREAM_STATES


class _Msg:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


class _Info:
    """Stand-in for paho's MQTTMessageInfo.

    Real paho (2.1.0): `wait_for_publish` raises RuntimeError immediately,
    before any waiting, whenever the message's rc is not 0 -- e.g. `publish()`
    was called on a disconnected socket (MQTT_ERR_NO_CONN). `fail=True`
    reproduces exactly that so a test can exercise it without a real broker.
    """

    def __init__(self, fail=False):
        self._fail = fail

    def wait_for_publish(self, timeout=None):
        if self._fail:
            raise RuntimeError("Message publish failed: No connection")


class _FakeBroker:
    """Stands in for paho's Client during teardown tests, so no real socket or
    broker is needed. `respond` controls what happens on connect():
    - "connack": call the stored on_connect callback with rc=0, as a broker
      that accepts the connection would.
    - "refused": raise ConnectionRefusedError, as an unreachable broker would.
    - "silent": return normally but never call on_connect, as a broker that
      accepts the TCP connection but never completes the handshake would.

    `disconnect_after`, if set, simulates the broker dropping the connection
    partway through teardown: the first `disconnect_after` publishes succeed,
    every one after that returns an `_Info` whose `wait_for_publish` raises.
    """

    def __init__(self, respond="connack", disconnect_after=None):
        self.respond = respond
        self.disconnect_after = disconnect_after
        self.on_connect = None
        self.connect_timeout = None
        self.connect_calls = 0
        self.keepalive_used = None
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.published = []
        self.publish_calls = 0

    def connect(self, host, port, keepalive=30):
        self.connect_calls += 1
        self.keepalive_used = keepalive
        if self.respond == "refused":
            raise ConnectionRefusedError("refused")
        if self.respond == "connack":
            self.on_connect(self, None, None, 0)
        # "silent": nothing happens; on_connect never fires.

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def publish(self, topic, payload, retain=False):
        self.publish_calls += 1
        if self.disconnect_after is not None and self.publish_calls > self.disconnect_after:
            return _Info(fail=True)
        self.published.append((topic, payload, retain))
        return _Info(fail=False)


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


class _RaisingClient(_FakeBroker):
    def subscribe(self, topic):
        raise OSError("socket gone")


def test_a_failing_subscribe_never_escapes_the_connect_callback(caplog):
    """The connect callback runs on paho's network thread too: an exception
    there tears the loop down and leaves the camera uncontrollable."""
    connected = []
    io = MqttIO(Settings(), on_connected=lambda: connected.append(True))
    with caplog.at_level(logging.ERROR):
        io._on_connect(_RaisingClient(), None, None, 0)
    assert connected == [True], "the state replay must still run"
    assert any("connect" in r.getMessage() for r in caplog.records)


def test_a_discovery_payload_that_cannot_be_built_never_escapes_the_connect_callback(
        monkeypatch, caplog):
    """discovery_payloads raises on a domain it has no renderer for, by design."""
    def boom(s):
        raise ValueError("x: no discovery renderer for domain 'fan'")

    monkeypatch.setattr(mqtt_io_module, "discovery_payloads", boom)
    connected = []
    io = MqttIO(Settings(), on_connected=lambda: connected.append(True))
    client = _FakeBroker()
    client.subscribe = lambda topic: None
    with caplog.at_level(logging.ERROR):
        io._on_connect(client, None, None, 0)
    assert connected == [True]
    assert any("connect" in r.getMessage() for r in caplog.records)


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
    # cable_state joins these as an enum: like stream_state it names a condition
    # rather than measuring a quantity, so it takes neither a unit nor a
    # state_class.
    not_a_measurement = {"stream_state", "last_frame", "last_still", "holders", "cable_state",
                         # How the last attempt to press Start streaming went,
                         # and how the last attempt to open the app went: both
                         # conditions, like the enums above them.
                         # ...and how the phone reboot went, same again.
                         "stream_start", "app_open", "phone_reboot",
                         "emergency_background",
                         # ...and whether a flat field is recorded.
                         "flat_field", "focus_run", "colour_reference", "horizon",
                         # ...and what the last timelapse request came to.
                         "timelapse"}
    unitless_by_design = {"subject_level", "lum_mean", "dynamic_range", "noise", "snr",
                          "focus_sharpness", "motion", "flat_field_scale"} | not_a_measurement
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
    # Sourced from states.STREAM_STATES, not restated, so this asserts the two
    # agree rather than freezing a copy that would need editing on every change.
    assert set(sensors["stream_state"]["options"]) == set(STREAM_STATES)
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


def test_teardown_clears_exactly_the_topics_discovery_would_publish():
    """EXPOSE_MQTT=false must delete every entity discovery created -- derived
    from the registry (discovery_payloads), never a hand list, so a new entity
    cannot be forgotten here."""
    io = MqttIO(Settings())
    io.c = _FakeBroker(respond="connack")
    io.teardown_discovery(timeout=0.2)
    expected_topics = {topic for topic, _cfg in discovery_payloads(Settings())}
    expected_topics.add(f"{Settings().mqtt_prefix}/available")
    published_topics = {topic for topic, _payload, _retain in io.c.published}
    assert published_topics == expected_topics
    for _topic, payload, retain in io.c.published:
        assert payload == "", "a non-empty payload would not delete the entity"
        assert retain is True, "a non-retained clear leaves the old retained config in place"


def test_teardown_retracts_the_retained_availability_last(caplog):
    """Left behind, the retained `online` has a broker announcing a device that
    no longer has any entities. Cleared after the entities, within the same
    deadline, so a teardown cut short never leaves entities with no
    availability topic behind them."""
    io = MqttIO(Settings())
    io.c = _FakeBroker(respond="connack")
    with caplog.at_level(logging.INFO):
        io.teardown_discovery(timeout=0.2)
    topic, payload, retain = io.c.published[-1]
    assert (topic, payload, retain) == (f"{Settings().mqtt_prefix}/available", "", True)
    total = len(discovery_payloads(Settings()))
    assert any(f"{total} entities removed" in r.getMessage() for r in caplog.records)


def test_teardown_makes_exactly_one_connection_attempt():
    io = MqttIO(Settings())
    io.c = _FakeBroker(respond="connack")
    io.teardown_discovery(timeout=0.2)
    assert io.c.connect_calls == 1


def test_teardown_never_raises_when_broker_refuses_connection(caplog):
    """A native-only deployment may have no broker at all; teardown must give up
    with one log line, not raise and not retry."""
    io = MqttIO(Settings())
    io.c = _FakeBroker(respond="refused")
    with caplog.at_level(logging.WARNING):
        io.teardown_discovery(timeout=0.2)
    assert io.c.connect_calls == 1
    assert io.c.published == []
    assert any("MQTT" in r.message for r in caplog.records)


def test_teardown_gives_up_once_when_the_broker_never_completes_the_handshake(caplog):
    """Bounded in time: a broker that accepts the TCP connection but never sends
    a CONNACK must not hang teardown forever."""
    io = MqttIO(Settings())
    io.c = _FakeBroker(respond="silent")
    with caplog.at_level(logging.WARNING):
        io.teardown_discovery(timeout=0.1)
    assert io.c.connect_calls == 1
    assert io.c.published == []
    assert io.c.loop_stopped is True
    assert io.c.disconnected is True
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "no response" in warnings[0]


def test_teardown_never_raises_when_the_connection_drops_mid_publish(caplog):
    """Reproduces paho 2.1.0's real behaviour, confirmed against the installed
    package: MQTTMessageInfo.wait_for_publish raises RuntimeError immediately
    (before any waiting) whenever the message's rc is not 0 -- e.g. the broker
    dropped the connection, or the client's own keepalive expired, partway
    through the discovery-topic loop. This must degrade to one log line
    reporting how many topics were cleared, never an uncaught exception."""
    total_topics = len(discovery_payloads(Settings()))
    io = MqttIO(Settings())
    io.c = _FakeBroker(respond="connack", disconnect_after=3)
    with caplog.at_level(logging.WARNING):
        io.teardown_discovery(timeout=5.0)  # must not raise
    assert len(io.c.published) == 3
    assert 3 < total_topics, "fixture assumption: more topics exist than the simulated cutoff"
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("3" in m for m in warnings), warnings


def test_teardown_stops_within_one_overall_deadline_not_per_topic_waits(monkeypatch):
    """A per-topic timeout would let N slow topics run to N * timeout -- with 80+
    discovery topics that is minutes, directly under "nothing optional may stop
    the container starting". The budget must cover the whole call. Simulated
    with a monkeypatched clock, not a real sleep of any length: each fake
    publish advances the clock by a second, and a 3.5s budget must not be
    enough for all topics."""
    clock = {"t": 0.0}
    monkeypatch.setattr(mqtt_io_module.time, "monotonic", lambda: clock["t"])

    class _SlowFakeBroker(_FakeBroker):
        def publish(self, topic, payload, retain=False):
            clock["t"] += 1.0
            return super().publish(topic, payload, retain=retain)

    total_topics = len(discovery_payloads(Settings()))
    io = MqttIO(Settings())
    io.c = _SlowFakeBroker(respond="connack")
    io.teardown_discovery(timeout=3.5)
    assert 0 < len(io.c.published) < total_topics, (
        "a per-topic (rather than overall) timeout would have cleared every topic here"
    )


def test_teardown_rejects_a_non_positive_timeout_without_raising(caplog):
    """paho's own `connect_timeout` setter raises ValueError for timeout <= 0
    (confirmed in the installed package); teardown must never raise, so a bad
    call here is one log line and no connection attempt, not a crash."""
    io = MqttIO(Settings())
    io.c = _FakeBroker(respond="connack")
    with caplog.at_level(logging.WARNING):
        io.teardown_discovery(timeout=0)
    assert io.c.connect_calls == 0
    assert any("timeout" in r.message.lower() for r in caplog.records)


def test_teardown_keeps_the_connection_alive_for_the_whole_budget():
    """The hardcoded keepalive=30 from the first draft could let the broker
    drop the connection mid-teardown on any budget longer than 30s. keepalive
    must cover at least the whole requested timeout."""
    io = MqttIO(Settings())
    io.c = _FakeBroker(respond="connack")
    io.teardown_discovery(timeout=45.0)
    assert io.c.keepalive_used >= 45.0


def test_teardown_disconnects_before_stopping_the_loop():
    """Matches close() (mqtt_io.py) which disconnects, then stops the loop --
    stopping the loop first would join the network thread before it can send
    the DISCONNECT packet."""
    order = []
    io = MqttIO(Settings())
    fb = _FakeBroker(respond="connack")
    fb.disconnect = lambda: order.append("disconnect")
    fb.loop_stop = lambda: order.append("loop_stop")
    io.c = fb
    io.teardown_discovery(timeout=0.2)
    assert order == ["disconnect", "loop_stop"]


def test_stream_state_declares_every_value_the_code_can_produce():
    """Home Assistant rejects a state that is not in an enum sensor's options,
    and does it silently -- the entity just goes unavailable. So the discovery
    payload has to list every value, including the ones only reachable on a
    backgrounded phone or an unplugged cable."""
    cfg = next(c for _, c in discovery_payloads(Settings())
               if c["default_entity_id"] == "sensor.ios2ha_camera_stream_state")
    assert set(cfg["options"]) == set(STREAM_STATES)
    for expected in ("ready", "backgrounded", "unavailable", "in_use", "capturing"):
        assert expected in cfg["options"]


# -- the stand-in used when MQTT is switched off -----------------------------

def test_the_stand_in_answers_every_call_the_service_makes():
    """The supervisor publishes unconditionally. Asking "is MQTT on?" at each of
    these call sites would put the flag in a dozen places that do not otherwise
    care about it; the answer is which object was handed over at startup."""
    io = NullMqttIO()
    io.connect()
    io.loop_start()
    io.publish_state({"phone_battery": 81})
    io.publish_state({"phone_battery": 81}, retain=False)
    io.publish_event({"error": "x"})
    io.publish_still(b"jpeg")
    io.publish_screenshot(b"png")
    io.set_available(True)
    io.loop_stop()
    io.close()
    io.close(timeout=0.1)


def test_the_stand_in_matches_the_real_client_call_for_call():
    """A method the real client grows and this one does not is an AttributeError
    at whatever hour the deployment with MQTT off next restarts."""
    real = {n for n in vars(MqttIO) if not n.startswith("_")}
    # teardown_discovery is the deliberate exception: clearing the retained
    # discovery topics needs a broker, so a copy here that quietly did nothing
    # would report success for entities that were never removed. __main__ uses a
    # real client for it.
    shared = real - {"teardown_discovery"}
    assert shared <= {n for n in vars(NullMqttIO) if not n.startswith("_")}
    assert not hasattr(NullMqttIO, "teardown_discovery")
    for name in sorted(shared):
        assert (inspect.signature(getattr(NullMqttIO, name))
                == inspect.signature(getattr(MqttIO, name))), name


def test_the_stand_in_needs_no_broker_settings():
    """It is chosen precisely when the broker settings are not being used, and
    must not be able to fail on one that is malformed."""
    assert inspect.signature(NullMqttIO) == inspect.Signature()
