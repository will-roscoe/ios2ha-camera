"""Timelapses in the service: every way in reaches the same builder, every way out
hears the same outcome.

`handle_control` is where MQTT set/{name} and POST /control/{name} both end. MQTT
has no reply, so a request it delivers is judged here and a refusal is published --
the `timelapse` state and an event -- where every client can see it.
"""
import io
import time

import pytest
from PIL import Image
from tests.test_supervisor import FakeArchive, FakeMqtt, Inline

from ios2ha_camera.archive import Archive
from ios2ha_camera.ring_buffer import RingBuffer
from ios2ha_camera.settings import Settings
from ios2ha_camera.supervisor import Supervisor
from ios2ha_camera.timelapse import TimelapseBuilder


def _sup(tmp_path, with_builder=True):
    s = Settings()
    sup = Supervisor(s, RingBuffer(1_000_000), FakeMqtt(), FakeArchive(),
                     device_executor=Inline(), sleep=lambda s: None)
    if with_builder:
        a = Archive(str(tmp_path / "archive"))
        now = time.time()
        for i in range(8):
            b = io.BytesIO()
            Image.new("RGB", (64, 48), (i * 30, 90, 150)).save(b, "JPEG")
            a.write_still(b.getvalue(), now - 3600 + i * 300)
        sup.timelapse = TimelapseBuilder(str(tmp_path / "out"), a.root,
                                         sup.publish_state, sup._event)
    return sup


def _events(sup, control):
    return [e for e in sup.mqtt.events if e.get("control") == control]


def test_a_spec_from_mqtt_or_the_api_is_built(tmp_path):
    sup = _sup(tmp_path)
    sup.handle_control("build_timelapse", {"range": "12h", "fps": 4})
    assert sup.mqtt.published["timelapse"] == "queued"
    assert sup.timelapse.run_once() == "built"
    p = sup.mqtt.published
    assert p["timelapse"] == "built" and p["timelapses"] == 1
    assert p["timelapse_catalogue"]["items"]["last_12h"]["fps"] == 4
    assert _events(sup, "build_timelapse")[-1]["result"] == "built"


@pytest.mark.parametrize("value, word", [("12h", "object"), ({"range": "9d"}, "range"),
                                         ({"range": "12h", "colour": 1}, "colour")])
def test_a_spec_mqtt_delivers_that_cannot_be_built_is_refused_out_loud(tmp_path, value, word):
    sup = _sup(tmp_path)
    sup.handle_control("build_timelapse", value)
    assert sup.mqtt.published["timelapse"] == "rejected_invalid"
    assert word in _events(sup, "build_timelapse")[-1]["reason"]
    assert sup.timelapse.run_once() is None


def test_a_range_button_builds_its_range(tmp_path):
    sup = _sup(tmp_path)
    sup.handle_control("build_timelapse_12h", None)
    assert sup.timelapse.run_once() == "built"
    assert "last_12h" in sup.mqtt.published["timelapse_catalogue"]["items"]


def test_delete(tmp_path):
    sup = _sup(tmp_path)
    sup.handle_control("build_timelapse_12h", None)
    sup.timelapse.run_once()
    sup.handle_control("delete_timelapse", {"name": "nothing"})
    assert sup.mqtt.published["timelapse"] == "rejected_unknown"
    sup.handle_control("delete_timelapse", "last_12h")
    assert sup.mqtt.published["timelapse"] == "rejected_invalid"
    sup.handle_control("delete_timelapse", {"name": "last_12h"})
    assert sup.mqtt.published["timelapse"] == "deleted"
    assert sup.mqtt.published["timelapse_catalogue"] == {"items": {}}


def test_switched_off_is_said_and_harmless(tmp_path, caplog):
    sup = _sup(tmp_path, with_builder=False)
    sup.handle_control("build_timelapse", {"range": "12h"})
    assert "TIMELAPSE_DIR" in caplog.text
    h = sup.health_state()
    assert (h["timelapse"], h["timelapses"], h["timelapse_catalogue"]) == ("off", 0, {"items": {}})
    assert "TIMELAPSE_DIR" in h["timelapse_last"]["reason"]


def test_with_timelapses_on_the_builder_alone_publishes_them(tmp_path):
    """The health tick snapshots and publishes later; with the builder publishing
    on its own thread that could put an old catalogue back after a new one."""
    sup = _sup(tmp_path)
    assert "timelapse" not in sup.health_state()
    assert sup.timelapse.state()["timelapse"] == "idle"
