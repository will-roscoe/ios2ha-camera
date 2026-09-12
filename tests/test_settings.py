from ios2ha_camera.settings import Settings


def test_defaults_match_spec():
    s = Settings()
    assert s.interval_s == 600
    assert s.render_intent == "natural"
    assert s.white_balance == "as-shot"
    assert s.stacking_mode == "auto"
    assert s.max_depth >= 1


def test_from_env_reads_connection():
    s = Settings.from_env({"PHONE_HOST": "192.168.1.11", "PHONE_PASSWORD": "x"})
    assert s.phone_host == "192.168.1.11"
    assert s.phone_password == "x"


def test_apply_control_known_and_unknown():
    s = Settings()
    assert s.apply_control("interval_s", 300) is True
    assert s.interval_s == 300
    assert s.apply_control("not_a_control", 1) is False


def test_from_env_coerces_numeric_fields():
    """Env values arrive as strings; ports must not reach aiohttp/paho as str."""
    s = Settings.from_env({"LIVE_PORT": "9001", "MQTT_PORT": "1884", "PHONE_PORT": "8081"})
    assert s.live_port == 9001
    assert s.mqtt_port == 1884
    assert s.phone_port == 8081


def test_from_env_leaves_unset_fields_at_default():
    s = Settings.from_env({})
    assert s.live_port == Settings().live_port
    assert s.archive_dir == Settings().archive_dir


def test_apply_control_sets_one_black_level_channel():
    """Discovery publishes set/black_level_{0,1,2}; each must land in one channel."""
    s = Settings()
    assert s.black_level_override is None
    assert s.apply_control("black_level_1", 0.46) is True
    assert s.black_level_override == (0.0, 0.46, 0.0)
    assert s.apply_control("black_level_0", 0.24) is True
    assert s.black_level_override == (0.24, 0.46, 0.0)


def test_apply_control_rejects_out_of_range_black_level_channel():
    s = Settings()
    assert s.apply_control("black_level_3", 1.0) is False
    assert s.black_level_override is None


def test_every_control_names_a_real_field():
    """A control that names no field would accept writes that go nowhere."""
    from ios2ha_camera.settings import CONTROLS

    names = {f.name for f in Settings.__dataclass_fields__.values()}
    assert CONTROLS <= names
