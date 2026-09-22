"""Asking Home Assistant whether the entities this service publishes are healthy.

Deliberately the *small* version of the idea in future-work section 5. That
section proposed creating config entries and installing automations, which needs
an admin token -- a container holding credentials for the whole house. The
native integration has since taken over the provisioning half, so what is left
worth having is the half that needs no privilege: after a deploy, did the
entities come back, and are any of them unavailable?

So the rules tested here are mostly about staying off: no configuration means no
network call at all, and nothing here ever writes.
"""

import json

import pytest

from ios2ha_camera.ha import HomeAssistant, summarise


def _state(entity_id, state="on"):
    return {"entity_id": entity_id, "state": state}


# -- the summary -------------------------------------------------------------

def test_counts_only_this_service_s_entities():
    states = [_state("sensor.ios2ha_camera_battery", "98"),
              _state("camera.ios2ha_camera_live", "idle"),
              _state("light.kitchen", "on")]
    s = summarise(states, prefix="ios2ha")
    assert s["total"] == 2
    assert s["unavailable"] == []
    assert s["ok"] is True


def test_unavailable_entities_are_named_not_just_counted():
    """"Three are unavailable" is not actionable; which three is."""
    states = [_state("sensor.ios2ha_camera_battery", "unavailable"),
              _state("sensor.ios2ha_camera_frame_rate", "13.9"),
              _state("binary_sensor.ios2ha_camera_camera_online", "unavailable")]
    s = summarise(states, prefix="ios2ha")
    assert s["unavailable"] == ["binary_sensor.ios2ha_camera_camera_online",
                                "sensor.ios2ha_camera_battery"]
    assert s["ok"] is False


def test_unknown_is_not_unavailable():
    """A control nobody has written yet reads `unknown` and is perfectly healthy;
    treating it as a fault would make a clean deploy look broken."""
    states = [_state("number.ios2ha_camera_black_level_0", "unknown")]
    s = summarise(states, prefix="ios2ha")
    assert s["unavailable"] == []
    assert s["ok"] is True


def test_no_entities_at_all_is_a_failure():
    """The integration is gone, or the prefix is wrong. Either way, not healthy."""
    s = summarise([_state("light.kitchen")], prefix="ios2ha")
    assert s["total"] == 0
    assert s["ok"] is False


# -- staying off unless configured ------------------------------------------

def test_not_configured_without_a_url_or_token():
    assert HomeAssistant("", "").configured is False
    assert HomeAssistant("http://ha.example:8123", "").configured is False
    assert HomeAssistant("", "token").configured is False
    assert HomeAssistant("http://ha.example:8123", "token").configured is True


def test_an_unconfigured_client_never_touches_the_network():
    """The whole feature is off by default, so this must be impossible to trip."""
    def explode(*a, **k):
        raise AssertionError("no request may be made without configuration")

    ha = HomeAssistant("", "", opener=explode)
    with pytest.raises(RuntimeError, match="not configured"):
        ha.states()


# -- the request itself ------------------------------------------------------

class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_sends_a_bearer_token_and_asks_for_states():
    seen = {}

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["method"] = req.get_method()
        return FakeResponse([_state("sensor.ios2ha_camera_battery")])

    ha = HomeAssistant("http://ha.example:8123", "secret", opener=opener)
    assert len(ha.states()) == 1
    assert seen["url"] == "http://ha.example:8123/api/states"
    assert seen["auth"] == "Bearer secret"
    # Never anything but a read: this holds someone's Home Assistant token.
    assert seen["method"] == "GET"


def test_a_trailing_slash_on_the_url_does_not_double_up():
    seen = {}

    def opener(req, timeout=None):
        seen["url"] = req.full_url
        return FakeResponse([])

    HomeAssistant("http://ha.example:8123/", "secret", opener=opener).states()
    assert seen["url"] == "http://ha.example:8123/api/states"


def test_the_token_is_never_logged_or_repeated_back():
    """It is admin-capable in the worst case, so it must not reach a log line."""
    ha = HomeAssistant("http://ha.example:8123", "secret")
    assert "secret" not in repr(ha)
    assert "secret" not in str(ha)


# -- the command line --------------------------------------------------------
#
# A deploy script acts on the exit status, so each one is pinned here: 0 healthy,
# 1 a real problem, 2 not configured. Conflating the last two would make an
# unconfigured check look like a broken deployment.

def _run(env, states=None, error=None, lines=None):
    from ios2ha_camera import ha

    def fake_states(self):
        if error:
            raise error
        return states or []

    original = ha.HomeAssistant.states
    ha.HomeAssistant.states = fake_states
    try:
        return ha.main(env=env, out=(lines.append if lines is not None else lambda *a: None))
    finally:
        ha.HomeAssistant.states = original


def test_exits_2_when_not_configured():
    lines = []
    assert _run({}, lines=lines) == 2
    assert "not configured" in lines[0]


def test_exits_0_when_everything_is_available():
    env = {"HA_URL": "http://ha.example:8123", "HA_TOKEN": "t"}
    lines = []
    assert _run(env, states=[_state("sensor.ios2ha_camera_battery", "98")], lines=lines) == 0
    assert "none unavailable" in lines[0]


def test_exits_1_and_names_the_unavailable_entities():
    env = {"HA_URL": "http://ha.example:8123", "HA_TOKEN": "t"}
    lines = []
    assert _run(env, states=[_state("sensor.ios2ha_camera_battery", "unavailable")],
                lines=lines) == 1
    assert any("sensor.ios2ha_camera_battery" in line for line in lines)


def test_exits_1_when_home_assistant_cannot_be_reached():
    env = {"HA_URL": "http://ha.example:8123", "HA_TOKEN": "t"}
    lines = []
    assert _run(env, error=OSError("connection refused"), lines=lines) == 1
    assert "could not reach" in lines[0]


def test_an_unreachable_check_does_not_leak_the_token():
    env = {"HA_URL": "http://ha.example:8123", "HA_TOKEN": "hunter2"}
    lines = []
    _run(env, error=OSError("connection refused"), lines=lines)
    assert not any("hunter2" in line for line in lines)
