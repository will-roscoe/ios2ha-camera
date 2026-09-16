import logging

import pytest

from ios2ha_camera.settings import _BOOL_CONTROLS, Settings, both_interfaces_off_message


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


@pytest.mark.parametrize("value", [None, [0.1], {"v": 0.1}])
def test_apply_control_rejects_a_non_number_for_one_black_level_channel(value):
    """float() raises TypeError for these, which the supervisor's corrective
    republish (it catches ValueError) would not see."""
    s = Settings()
    with pytest.raises(ValueError):
        s.apply_control("black_level_1", value)
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


def test_expose_flags_default_true():
    s = Settings()
    assert s.expose_mqtt is True
    assert s.expose_api is True


@pytest.mark.parametrize("env_key,attr", [("EXPOSE_MQTT", "expose_mqtt"), ("EXPOSE_API", "expose_api")])
@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("False", False), ("0", False), ("no", False), ("off", False),
])
def test_expose_flags_accept_conventional_spellings(env_key, attr, raw, expected):
    s = Settings.from_env({env_key: raw})
    assert getattr(s, attr) is expected


@pytest.mark.parametrize("env_key,attr", [("EXPOSE_MQTT", "expose_mqtt"), ("EXPOSE_API", "expose_api")])
def test_expose_flag_typo_keeps_default_and_warns(env_key, attr, caplog):
    """`_coerce_like` treats any string outside `_TRUTHY` as False, which would
    make EXPOSE_MQTT=ture switch MQTT off without a word and nothing in the
    log to explain why. These two flags must keep their default and warn
    instead of guessing, and must never raise: nothing optional may stop the
    container starting."""
    with caplog.at_level(logging.WARNING):
        s = Settings.from_env({env_key: "ture"})
    assert getattr(s, attr) is True  # default, unchanged
    assert any(env_key in r.message and "ture" in r.message for r in caplog.records)


def test_only_the_expose_flags_get_the_keep_default_treatment(monkeypatch):
    """Only EXPOSE_MQTT/EXPOSE_API keep their default and warn on a typo; every
    other key in _ENV_MAP must still take _coerce_like's older, blunter branch,
    where any string outside _TRUTHY becomes False.

    None of today's other _ENV_MAP entries default to bool -- checked directly:
    PHONE_PORT/MQTT_PORT/LIVE_PORT are int, the rest (PHONE_HOST, MQTT_URL,
    MQTT_HOST, MQTT_USER, MQTT_PASSWORD, MQTT_PREFIX, ARCHIVE_DIR, ...) are str
    -- so there is no live boolean env var to round-trip through
    Settings.from_env unmodified. This proves the from_env dispatch itself
    (only _EXPOSE_ENV_KEYS take the new branch) against a genuine boolean
    dataclass field, `autofocus`, mapped in for the duration of the test.
    """
    import ios2ha_camera.settings as settings_module

    monkeypatch.setitem(settings_module._ENV_MAP, "AUTOFOCUS_ENV_TEST_ONLY", "autofocus")
    s = Settings.from_env({"AUTOFOCUS_ENV_TEST_ONLY": "ture"})
    assert s.autofocus is False  # unchanged: _coerce_like still guesses False on a typo


def test_both_interfaces_off_message_names_what_is_off_and_what_still_works():
    msg = both_interfaces_off_message()
    assert "EXPOSE_MQTT" in msg and "EXPOSE_API" in msg
    for still_working in ("capture", "archive", "/live", "/live/stacked", "/still.jpg"):
        assert still_working in msg, f"{still_working!r} missing from: {msg!r}"


def test_apply_control_rejects_a_non_iterable_black_level_override():
    """tuple(float(v) for v in value) raises TypeError on a bare number, which
    Supervisor._apply_setting's `except ValueError` does not catch --
    the corrective republish meant for exactly this kind of rejected write never
    runs. Must surface as ValueError instead."""
    s = Settings()
    with pytest.raises(ValueError):
        s.apply_control("black_level_override", 1.0)
    assert s.black_level_override is None
