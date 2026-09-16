import pytest

from ios2ha_camera.settings import _BOOL_CONTROLS, Settings


def test_defaults_match_spec():
    s = Settings()
    assert s.interval_s == 600
    assert s.render_intent == "natural"
    assert s.white_balance == "as-shot"
    assert s.stacking_mode == "auto"
    assert s.max_depth >= 1


def test_live_max_window_s_default():
    assert Settings().live_max_window_s == 3.0


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


def test_apply_control_rejects_a_value_outside_a_selects_options():
    import pytest

    s = Settings()
    for name, bad in [("white_balance", "sepia"), ("render_intent", "artistic"),
                      ("stacking_mode", "maximum")]:
        with pytest.raises(ValueError):
            s.apply_control(name, bad)
    assert s.white_balance == "as-shot"
    assert s.render_intent == "natural"
    assert s.stacking_mode == "auto"


def test_apply_control_accepts_every_declared_option():
    s = Settings()
    for value in ("off", "fixed", "auto"):
        assert s.apply_control("stacking_mode", value) is True
    for value in ("as-shot", "grey-world", "brightest-patch"):
        assert s.apply_control("white_balance", value) is True
    for value in ("natural", "inspection"):
        assert s.apply_control("render_intent", value) is True


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


def test_the_on_demand_controls_are_writable():
    s = Settings()
    assert s.on_demand is True and s.live_max_window_s == 3.0
    assert s.apply_control("on_demand", False) is True
    assert s.on_demand is False
    assert s.apply_control("live_max_window_s", 5.0) is True
    assert s.live_max_window_s == 5.0


def test_bool_controls_are_derived_from_the_dataclass():
    """Exactly the CONTROLS whose field defaults to an actual bool -- not a
    hardcoded list that could silently drift from the dataclass."""
    assert _BOOL_CONTROLS == {
        "stacking_allowed", "on_demand", "autofocus", "backgrounding", "auto_start",
    }


@pytest.mark.parametrize("name", sorted(_BOOL_CONTROLS))
@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("TRUE", True), ("1", True),
    ("yes", True), ("on", True), (True, True),
    ("false", False), ("False", False), ("0", False),
    ("no", False), ("off", False), (False, False),
])
def test_apply_control_coerces_conventional_boolean_spellings(name, raw, expected):
    """A non-JSON publisher (mosquitto_pub, Node-RED, a hand-typed script) can send
    these as bare strings, not JSON booleans -- they must land as the same bool
    Home Assistant's own switch UI would have sent as real JSON true/false."""
    s = Settings()
    assert s.apply_control(name, raw) is True
    assert getattr(s, name) is expected


@pytest.mark.parametrize("name", sorted(_BOOL_CONTROLS))
def test_apply_control_rejects_unparseable_boolean_junk(name):
    """json.loads fails on a bare `off`/`False`/`no`, so mqtt_io hands apply_control
    the raw string; storing it unchanged makes `not "off"` False -- the opposite of
    what was sent. A value that cannot be interpreted must be rejected outright."""
    s = Settings()
    before = getattr(s, name)
    with pytest.raises(ValueError):
        s.apply_control(name, "banana")
    assert getattr(s, name) == before  # a rejected write must not touch the stored value


def test_apply_control_rejects_a_non_iterable_black_level_override():
    """tuple(float(v) for v in value) raises TypeError on a bare number, which
    _apply_setting's `except ValueError` (supervisor.py:411-424) does not catch --
    the corrective republish meant for exactly this kind of rejected write never
    runs. Must surface as ValueError instead."""
    s = Settings()
    with pytest.raises(ValueError):
        s.apply_control("black_level_override", 1.0)
    assert s.black_level_override is None
