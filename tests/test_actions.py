"""Actions: writes whose value is a JSON object, on the same control path as the rest.

Will: "every interface should be run in/out via the shared api system so that
mqtt/native/webui all have full access to the same systems." So a timelapse build is
not a route of its own: it is declared once, served in /objects with the fields a
client needs to build a form, and reached by POST /control/{name} or MQTT set/{name}.
"""
from tests.test_api import _get, _post, _posts, _recording_api

from ios2ha_camera.api import descriptors
from ios2ha_camera.entities import ACTIONS, ENTITIES, MEDIA, Entity
from ios2ha_camera.mqtt_io import _sensor_cfg
from ios2ha_camera.timelapse import SpecError, parse_delete


def test_the_actions_are_served_with_their_fields():
    doc = descriptors()
    actions = {a["name"]: a for a in doc["actions"]}
    assert set(actions) == {"build_timelapse", "delete_timelapse"}
    fps = actions["build_timelapse"]["fields"]["fps"]
    assert (fps["type"], fps["min"], fps["max"]) == ("int", 1, 60)
    assert "default" not in fps            # chosen for about ten seconds when left out
    assert actions["build_timelapse"]["fields"]["range"]["options"] == ["12h", "24h", "7d", "1mo"]
    assert actions["delete_timelapse"]["fields"]["name"]["type"] == "str"
    assert all(a["description"] for a in doc["actions"])
    assert [a.name for a in ACTIONS] == [a["name"] for a in doc["actions"]]


def test_every_object_says_where_its_attributes_are():
    assert all("attributes_key" in o for o in descriptors()["objects"])
    status, body = _get(_recording_api()[0], "/api/v1/objects")
    assert status == 200 and "actions" in body


def test_an_action_reaches_handle_control_as_the_object_sent():
    app, seen = _recording_api()
    status, body = _post(app, "/api/v1/control/build_timelapse",
                         json={"value": {"range": "7d", "fps": 24}})
    assert status == 200 and body["ok"]
    assert seen == [("build_timelapse", {"range": "7d", "fps": 24})]


def test_a_spec_that_cannot_be_built_is_refused_with_why():
    app, seen = _recording_api()
    (s1, b1), (s2, b2) = _posts(app,
                                ("/api/v1/control/build_timelapse", {"json": {"value": {"range": "9d"}}}),
                                ("/api/v1/control/build_timelapse", {"json": {"value": "7d"}}))
    assert s1 == 400 and "range" in b1["error"]
    assert s2 == 400 and "object" in b2["error"]
    assert seen == []


def test_delete_needs_a_name():
    app, seen = _recording_api()
    path = "/api/v1/control/delete_timelapse"
    got = _posts(app, (path, {"json": {"value": {}}}),
                 (path, {"json": {"value": {"name": "../x"}}}),
                 (path, {"json": {"value": {"name": "last_7d"}}}))
    assert [status for status, _ in got] == [400, 400, 200]
    assert seen == [("delete_timelapse", {"name": "last_7d"})]


def test_parse_delete():
    assert parse_delete({"name": "storm"}) == "storm"
    for bad in ("storm", {"name": "Bad"}, {"name": "x", "also": 1}, {}):
        try:
            parse_delete(bad)
        except SpecError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_an_unknown_name_still_lists_the_actions():
    app, _ = _recording_api()
    status, body = _post(app, "/api/v1/control/build_timelapses", json={"value": {}})
    assert status == 404 and "build_timelapse" in body["accepted"]


def test_attributes_reach_mqtt_discovery():
    e = Entity(object_id="timelapses", domain="sensor", name="Timelapses",
               state_key="timelapses", attributes_key="timelapse_catalogue")
    cfg = _sensor_cfg("ios2ha_camera", e)
    assert cfg["json_attributes_topic"] == "ios2ha_camera/state/timelapse_catalogue"
    assert "json_attributes_topic" not in _sensor_cfg("ios2ha_camera", ENTITIES[-1])


def test_timelapses_are_a_video_media_source():
    m = {x.id: x for x in MEDIA}["timelapse"]
    assert (m.kind, m.formats, m.path) == ("video", ("mp4",), "/api/v1/timelapse/{name}.mp4")
    assert m.params == {} and not m.holds_phone
