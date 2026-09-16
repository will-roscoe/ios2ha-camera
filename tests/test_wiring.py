"""The two optional interfaces, as startup decides them.

`main()` runs the service forever and cannot be called from a test, so the
flag-dependent part of it lives in small functions that can. What is pinned here
is the promise the flags make: neither interface, in any combination, may stop
the service starting, and whatever is switched off leaves no trace but a log
line -- no connection, no routes, and above all no retry.
"""

import asyncio
import logging
import threading

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ios2ha_camera import __main__ as main_module
from ios2ha_camera.live import make_live_app
from ios2ha_camera.live_stack import LiveStacker
from ios2ha_camera.mqtt_io import MqttIO, NullMqttIO
from ios2ha_camera.ring_buffer import RingBuffer
from ios2ha_camera.settings import Settings, both_interfaces_off_message


class _FakeMqtt:
    """Records the lifecycle calls `_start_interfaces` may make on the client."""

    def __init__(self):
        self.calls = []

    def connect(self):
        self.calls.append("connect")

    def loop_start(self):
        self.calls.append("loop_start")


class _FakeSupervisor:
    def __init__(self, state=None):
        self.state = dict(state or {})
        self.listeners = []
        self.controls_published = 0

    def current_state(self):
        return dict(self.state)

    def add_state_listener(self, fn):
        self.listeners.append(fn)

    def publish_controls(self):
        self.controls_published += 1

    def handle_control(self, name, value):
        return {"ok": True}


def _paths(app):
    return {r.resource.canonical for r in app.router.routes()}


def _lines(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "ios2ha_camera.__main__"]


def _start(s, sup=None, app=None, mqtt=None):
    return main_module._start_interfaces(s, sup or _FakeSupervisor(),
                                         app if app is not None else web.Application(),
                                         mqtt or _FakeMqtt())


# -- which client the supervisor publishes to --------------------------------

def test_mqtt_on_hands_the_supervisor_the_real_client():
    assert isinstance(main_module._mqtt_for(Settings(expose_mqtt=True)), MqttIO)


def test_mqtt_off_hands_the_supervisor_a_stand_in_and_reads_no_broker_setting():
    """The stand-in is chosen without building a real client, so a deployment
    that has switched MQTT off cannot be stopped from starting by broker
    settings it is no longer using."""
    s = Settings(expose_mqtt=False, mqtt_url="not://a valid url at all")
    assert isinstance(main_module._mqtt_for(s), NullMqttIO)


# -- MQTT on -----------------------------------------------------------------

def test_mqtt_on_connects_and_starts_the_network_loop(caplog):
    mqtt = _FakeMqtt()
    with caplog.at_level(logging.INFO):
        _start(Settings(expose_mqtt=True, expose_api=False), mqtt=mqtt)
    assert mqtt.calls == ["connect", "loop_start"]
    assert "MQTT discovery: on" in _lines(caplog)


def test_mqtt_on_does_not_clear_the_discovery_topics(monkeypatch):
    cleared = []
    monkeypatch.setattr(main_module, "_clear_discovery", lambda s: cleared.append(s))
    _start(Settings(expose_mqtt=True, expose_api=False))
    assert cleared == []


def test_the_control_values_are_remembered_whatever_the_flags_say():
    """The API answers from what the supervisor has published, and reaching the
    broker is what normally publishes the controls first. That leaves them
    absent while the broker is down, and for good when MQTT is off."""
    for mqtt_on in (True, False):
        for api_on in (True, False):
            sup = _FakeSupervisor()
            _start(Settings(expose_mqtt=mqtt_on, expose_api=api_on), sup=sup)
            assert sup.controls_published == 1, (mqtt_on, api_on)


# -- MQTT off ----------------------------------------------------------------

def test_mqtt_off_never_touches_the_broker(caplog):
    mqtt = _FakeMqtt()
    with caplog.at_level(logging.INFO):
        _start(Settings(expose_mqtt=False, expose_api=False), mqtt=mqtt)
    assert mqtt.calls == []
    assert "MQTT discovery: off, clearing discovery topics" in _lines(caplog)


def test_mqtt_off_clears_discovery_on_a_daemon_thread(monkeypatch):
    """Clearing makes one bounded attempt at a broker that may not exist. On the
    startup thread that wait would delay capture, health and the live view."""
    done = threading.Event()
    seen = {}

    def _record(s):
        seen["thread"] = threading.current_thread()
        done.set()

    monkeypatch.setattr(main_module, "_clear_discovery", _record)
    _start(Settings(expose_mqtt=False, expose_api=False))
    assert done.wait(5.0)
    assert seen["thread"] is not threading.current_thread()
    assert seen["thread"].daemon


def test_clearing_discovery_cannot_stop_the_service(caplog):
    """Everything about it is optional: an unparseable broker setting raises in
    the client's constructor, which must cost a log line and nothing more."""
    s = Settings(expose_mqtt=False, mqtt_url="not://a valid url at all")
    with caplog.at_level(logging.INFO):
        main_module._clear_discovery(s)
    assert any("discovery" in line for line in _lines(caplog))


# -- API on ------------------------------------------------------------------

def test_api_on_registers_the_json_routes(caplog):
    app = web.Application()
    with caplog.at_level(logging.INFO):
        _start(Settings(expose_api=True, expose_mqtt=True), app=app)
    assert {"/api/v1/info", "/api/v1/state", "/api/v1/events"} <= _paths(app)
    assert "JSON API: on at /api/v1/info" in _lines(caplog)


def test_api_on_answers_state_from_what_the_supervisor_remembers():
    """Not health_state(): that takes fresh measurements, carries no control
    values, and would block the event loop the live view is served on."""
    sup = _FakeSupervisor({"exposure": 0.5, "phone_battery": 81})
    app = web.Application()
    _start(Settings(expose_api=True), sup=sup, app=app)

    async def run():
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/v1/state")
            return await resp.json()

    assert asyncio.run(run()) == {"exposure": 0.5, "phone_battery": 81}


def test_api_on_is_fed_from_the_same_publish_mqtt_is():
    """One listener on publish_state, so the stream and the broker cannot end up
    reporting different values for the same key."""
    sup = _FakeSupervisor()
    hub = _start(Settings(expose_api=True), sup=sup)
    assert [fn.__self__ for fn in sup.listeners] == [hub]
    assert [fn.__func__ for fn in sup.listeners] == [type(hub).publish]


# -- API off -----------------------------------------------------------------

def test_api_off_registers_nothing_and_builds_no_hub(caplog):
    sup = _FakeSupervisor()
    app = web.Application()
    with caplog.at_level(logging.INFO):
        assert _start(Settings(expose_api=False), sup=sup, app=app) is None
    assert _paths(app) == set()
    assert sup.listeners == []
    assert "JSON API: off" in _lines(caplog)


def test_api_off_still_serves_every_media_route():
    """The media routes are the camera. They are never gated by a flag."""
    app = make_live_app(RingBuffer(100_000))
    before = _paths(app)
    _start(Settings(expose_api=False, expose_mqtt=False), app=app)
    assert _paths(app) == before
    assert {"/live", "/still.jpg"} <= before


def test_the_api_mounts_onto_the_application_already_serving_the_media():
    """One port, one application: the media routes are registered first and the
    JSON routes are added to the same router. A path claimed twice raises at
    startup, which is exactly the failure that must not be possible here."""
    buf = RingBuffer(100_000)
    s = Settings(expose_api=True)
    # As the service builds it: the stacked feed is only routed when there is a
    # stacker, and the API advertises it either way.
    app = make_live_app(buf, latest_still=lambda: None, stacker=LiveStacker(buf, s))
    media = _paths(app)
    _start(s, app=app)
    assert media < _paths(app)
    assert {"/api/v1/info", "/api/v1/state", "/api/v1/events"} <= _paths(app)

    async def run():
        async with TestClient(TestServer(app)) as client:
            return await (await client.get("/api/v1/media")).json()

    # Every media source the API advertises is served by this same application,
    # which is the whole reason for mounting rather than a second port.
    assert {m["url"] for m in asyncio.run(run())["media"]} <= _paths(app)


# -- the four combinations ---------------------------------------------------

def test_both_on_logs_one_line_each_and_no_warning(caplog):
    with caplog.at_level(logging.INFO):
        _start(Settings(expose_mqtt=True, expose_api=True))
    assert _lines(caplog) == ["MQTT discovery: on", "JSON API: on at /api/v1/info"]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_only_mqtt_logs_one_line_each_and_no_warning(caplog):
    with caplog.at_level(logging.INFO):
        _start(Settings(expose_mqtt=True, expose_api=False))
    assert _lines(caplog) == ["MQTT discovery: on", "JSON API: off"]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_only_the_api_logs_one_line_each_and_no_warning(caplog, monkeypatch):
    monkeypatch.setattr(main_module, "_clear_discovery", lambda s: None)
    with caplog.at_level(logging.INFO):
        _start(Settings(expose_mqtt=False, expose_api=True))
    assert _lines(caplog) == ["MQTT discovery: off, clearing discovery topics",
                              "JSON API: on at /api/v1/info"]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_both_off_warns_once_and_still_returns(caplog, monkeypatch):
    monkeypatch.setattr(main_module, "_clear_discovery", lambda s: None)
    with caplog.at_level(logging.INFO):
        assert _start(Settings(expose_mqtt=False, expose_api=False)) is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [both_interfaces_off_message()]
