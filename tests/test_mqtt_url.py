"""MQTT_URL: one scheme-based setting for the broker, with a fallback list.

The scheme carries transport, TLS and the default port, so there is no matrix of
MQTT_PORT / MQTT_TLS / MQTT_TRANSPORT variables to keep consistent. Credentials stay
in MQTT_USER / MQTT_PASSWORD so they can never be echoed in a log line or an error.
"""

import logging

import pytest

from ios2ha_camera import mqtt_io
from ios2ha_camera.mqtt_io import MqttIO
from ios2ha_camera.settings import MqttEndpoint, Settings, mqtt_endpoints, parse_mqtt_urls

# -- parsing -------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("mqtt://broker", MqttEndpoint("mqtt", "broker", 1883, "tcp", False, "/mqtt")),
    ("mqtt://broker:1884", MqttEndpoint("mqtt", "broker", 1884, "tcp", False, "/mqtt")),
    ("mqtts://broker.lan", MqttEndpoint("mqtts", "broker.lan", 8883, "tcp", True, "/mqtt")),
    ("ws://192.168.1.5:1884", MqttEndpoint("ws", "192.168.1.5", 1884, "websockets", False, "/mqtt")),
    ("wss://broker.example", MqttEndpoint("wss", "broker.example", 443, "websockets", True, "/mqtt")),
    ("wss://broker.example/custom", MqttEndpoint("wss", "broker.example", 443, "websockets", True, "/custom")),
])
def test_each_scheme_implies_its_transport_tls_and_port(url, expected):
    assert parse_mqtt_urls(url) == [expected]


def test_a_space_separated_list_keeps_its_order():
    eps = parse_mqtt_urls("mqtt://primary  mqtt://fallback:1884")
    assert [(e.host, e.port) for e in eps] == [("primary", 1883), ("fallback", 1884)]


def test_credentials_in_the_url_are_refused_without_echoing_them():
    """They belong in MQTT_USER / MQTT_PASSWORD. An error that quoted the URL
    would print the password into the container log."""
    with pytest.raises(ValueError) as e:
        parse_mqtt_urls("mqtt://ios2ha:hunter2@broker")
    assert "hunter2" not in str(e.value)
    assert "MQTT_USER" in str(e.value)


def test_a_mixed_scheme_list_is_refused():
    """Transport and TLS are fixed when the paho client is built, so a fallback
    list cannot switch between, say, plain TCP and websockets over TLS."""
    with pytest.raises(ValueError, match="scheme"):
        parse_mqtt_urls("mqtt://a wss://b")


@pytest.mark.parametrize("bad", ["http://broker", "broker:1883", "mqtt://", "mqtt://:1883", "   "])
def test_malformed_entries_are_refused(bad):
    with pytest.raises(ValueError):
        parse_mqtt_urls(bad)


# -- where the endpoints come from ----------------------------------------------


def test_mqtt_url_is_read_from_the_environment():
    s = Settings.from_env({"MQTT_URL": "mqtts://broker.lan"})
    assert mqtt_endpoints(s) == [MqttEndpoint("mqtts", "broker.lan", 8883, "tcp", True, "/mqtt")]


def test_without_mqtt_url_the_deprecated_host_and_port_still_work():
    """The running deployment sets MQTT_HOST today; it must keep working."""
    s = Settings.from_env({"MQTT_HOST": "10.0.0.9", "MQTT_PORT": "1884"})
    assert mqtt_endpoints(s) == [MqttEndpoint("mqtt", "10.0.0.9", 1884, "tcp", False, "/mqtt")]


def test_with_nothing_set_the_defaults_apply():
    assert mqtt_endpoints(Settings()) == [MqttEndpoint("mqtt", "192.168.1.5", 1883, "tcp", False, "/mqtt")]


def test_mqtt_url_wins_over_the_aliases_and_says_so(caplog):
    s = Settings.from_env({"MQTT_URL": "mqtt://new", "MQTT_HOST": "old", "MQTT_PORT": "1884"})
    with caplog.at_level(logging.WARNING):
        eps = mqtt_endpoints(s)
    assert [e.host for e in eps] == ["new"]
    assert "MQTT_HOST" in caplog.text and "MQTT_PORT" in caplog.text


def test_the_aliases_alone_do_not_warn(caplog):
    """A healthy run is quiet: a deployment that has not migrated yet is not a fault."""
    s = Settings.from_env({"MQTT_HOST": "old"})
    with caplog.at_level(logging.WARNING):
        mqtt_endpoints(s)
    assert caplog.records == []


# -- the paho client ------------------------------------------------------------


class FakeClient:
    """Records how MqttIO configures paho, through its public API only."""

    def __init__(self, *args, transport="tcp", **kwargs):
        self.transport = transport
        self.calls = []
        self.host = self.port = None

    def username_pw_set(self, *a): self.calls.append(("username_pw_set", a))
    def will_set(self, *a, **k): self.calls.append(("will_set", a))
    def tls_set(self, *a, **k): self.calls.append(("tls_set", a))
    def ws_set_options(self, path="/mqtt", **k): self.calls.append(("ws_set_options", path))

    def connect_async(self, host, port, keepalive=60):
        self.host, self.port = host, port
        self.calls.append(("connect_async", host, port, keepalive))


def _io(monkeypatch, **env):
    monkeypatch.setattr(mqtt_io.mqtt, "Client", FakeClient)
    return MqttIO(Settings.from_env(env))


def test_plain_mqtt_uses_tcp_without_tls(monkeypatch):
    io = _io(monkeypatch, MQTT_URL="mqtt://broker")
    io.connect()
    assert io.c.transport == "tcp"
    assert not any(c[0] in ("tls_set", "ws_set_options") for c in io.c.calls)
    assert ("connect_async", "broker", 1883, 30) in io.c.calls


def test_mqtts_turns_on_tls(monkeypatch):
    io = _io(monkeypatch, MQTT_URL="mqtts://broker")
    io.connect()
    assert io.c.transport == "tcp"
    assert any(c[0] == "tls_set" for c in io.c.calls)
    assert ("connect_async", "broker", 8883, 30) in io.c.calls


def test_wss_uses_websockets_tls_and_the_url_path(monkeypatch):
    io = _io(monkeypatch, MQTT_URL="wss://broker.example/custom")
    io.connect()
    assert io.c.transport == "websockets"
    assert any(c[0] == "tls_set" for c in io.c.calls)
    assert ("ws_set_options", "/custom") in io.c.calls
    assert ("connect_async", "broker.example", 443, 30) in io.c.calls


def test_a_failed_connection_moves_to_the_next_endpoint_and_wraps(monkeypatch):
    io = _io(monkeypatch, MQTT_URL="mqtt://primary mqtt://fallback:1884")
    io.connect()
    assert (io.c.host, io.c.port) == ("primary", 1883)
    io._on_connect_fail(io.c, None)
    assert (io.c.host, io.c.port) == ("fallback", 1884)
    io._on_connect_fail(io.c, None)
    assert (io.c.host, io.c.port) == ("primary", 1883)


def test_a_single_endpoint_never_rotates(monkeypatch):
    io = _io(monkeypatch, MQTT_URL="mqtt://only")
    io.connect()
    io._on_connect_fail(io.c, None)
    assert (io.c.host, io.c.port) == ("only", 1883)


def test_the_failure_callback_is_registered(monkeypatch):
    """A rotation that paho never calls rotates nothing."""
    io = _io(monkeypatch, MQTT_URL="mqtt://a mqtt://b")
    assert getattr(io.c, "on_connect_fail", None) == io._on_connect_fail
