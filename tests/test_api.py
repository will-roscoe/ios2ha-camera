"""The JSON API, pinned as a contract.

A Home Assistant custom component in its own repository, on its own release
cycle, is written against these exact responses: it hardcodes `/api/v1/info` and
discovers every other path from what that document says. So the assertions here
are about the served document rather than about Python objects -- a key that
quietly stops being served, or a path a client would then be forced to hardcode,
is a breaking change for a repository this suite cannot see and cannot fix.
"""

import asyncio
import json
import os
import subprocess
import sys

from aiohttp.test_utils import TestClient, TestServer

from ios2ha_camera.api import make_api, objects_version
from ios2ha_camera.entities import ENTITIES, MEDIA
from ios2ha_camera.mqtt_io import discovery_payloads
from ios2ha_camera.settings import Settings


def _get(app, path):
    """Status and decoded body of one GET, driven the way tests/test_live.py drives
    the loop -- pytest-aiohttp is not installed and is not being added."""
    async def run():
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(path)
            return resp.status, await resp.json()
    return asyncio.run(run())


def _api(state=dict, **kw):
    return make_api(state, Settings(), **kw)


def test_info_names_every_other_route():
    """The only path a client is allowed to hardcode. Everything else is
    discovered, so a missing key here strands the component."""
    status, info = _get(_api(), "/api/v1/info")
    assert status == 200
    assert info["interfaces"] == {"api": 1, "mqtt": 1}
    assert set(info["routes"]) >= {"objects", "state", "events", "control", "media"}
    assert all(p.startswith("/api/v1/") for p in info["routes"].values())
    # Templated rather than a prefix a caller would have to concatenate itself.
    assert info["routes"]["control"] == "/api/v1/control/{name}"


def test_info_carries_the_device_block_mqtt_publishes():
    """One declaration, two renderers. Two hand-kept copies of the device block is
    the same failure the entity registry exists to prevent, one level down."""
    _, info = _get(_api(), "/api/v1/info")
    published = discovery_payloads(Settings())[0][1]["device"]
    assert info["device"] == published
    assert list(info["device"]) == list(published)
    assert info["device"]["sw_version"].startswith(info["version"])


def test_objects_describes_every_entity():
    _, doc = _get(_api(), "/api/v1/objects")
    assert len(doc["objects"]) == len(ENTITIES) == 86
    assert [o["object_id"] for o in doc["objects"]] == [e.object_id for e in ENTITIES]

    by_key = {(o["domain"], o["object_id"]): o for o in doc["objects"]}
    assert by_key[("number", "interval_s")] == {
        "object_id": "interval_s", "domain": "number", "name": "Capture interval",
        "state_key": "interval_s", "device_class": "duration", "unit": "s",
        "state_class": None, "options": None, "lo": 30, "hi": 3600, "step": 1,
        "category": "config", "icon": "mdi:timer-outline", "writable": True,
        "precision": None,
    }
    assert by_key[("select", "white_balance")]["options"] == [
        "as-shot", "grey-world", "brightest-patch"]
    assert by_key[("sensor", "battery")]["state_class"] == "measurement"
    # The one null name in the registry: Home Assistant reads it as "this is the
    # device's main feature". Serving it as a real null, not as an absent key.
    assert by_key[("camera", "still")]["name"] is None
    assert by_key[("button", "capture_now")]["state_key"] is None


def test_every_descriptor_carries_every_key():
    """Uniform keys, nulls where a field does not apply: a client that has to test
    for a key's presence before reading it grows a different shape per domain."""
    _, doc = _get(_api(), "/api/v1/objects")
    keys = {tuple(sorted(o)) for o in doc["objects"]}
    assert len(keys) == 1


def test_an_object_is_identified_by_domain_and_object_id_together():
    """`screenshot` is both a camera and a button. A component keying its entities
    on object_id alone would silently drop one of the two."""
    _, doc = _get(_api(), "/api/v1/objects")
    ids = [o["object_id"] for o in doc["objects"]]
    pairs = [(o["domain"], o["object_id"]) for o in doc["objects"]]
    assert len(set(ids)) < len(ids)
    assert len(set(pairs)) == len(pairs) == 86


def test_media_declares_whether_it_holds_the_phone():
    """Capability plus cost: the snapshot is free, a stream keeps the phone's
    radio up for as long as a viewer stays attached."""
    _, doc = _get(_api(), "/api/v1/media")
    assert doc["media"] == [
        {"id": "live", "kind": "stream", "formats": ["mjpeg"],
         "url": "/api/v1/stream/live", "holds_phone": True, "params": {}},
        {"id": "stacked", "kind": "stream", "formats": ["mjpeg"],
         "url": "/api/v1/stream/stacked", "holds_phone": True, "params": {}},
        {"id": "still", "kind": "snapshot", "formats": ["jpeg"],
         "url": "/api/v1/snapshot/still", "holds_phone": False, "params": {}},
    ]
    assert len(doc["media"]) == len(MEDIA)


def test_objects_embeds_the_same_media_list_that_media_serves():
    _, objects = _get(_api(), "/api/v1/objects")
    _, media = _get(_api(), "/api/v1/media")
    assert objects["media"] == media["media"]


def test_state_is_whatever_the_state_callable_returns():
    """`api` is given a callable rather than the supervisor, which is what keeps it
    testable with a dict and stops it growing a second opinion about the state."""
    snapshot = {"battery": 88, "stream_state": "streaming", "last_frame": None}
    _, doc = _get(_api(lambda: snapshot), "/api/v1/state")
    assert doc == snapshot


def test_objects_version_is_stable_across_processes():
    """A reconnecting client compares this against its cached copy to decide
    whether to refetch, so a value that changed per run would make the cache
    useless and a value that never changed would make it wrong."""
    _, doc = _get(_api(), "/api/v1/objects")
    _, info = _get(_api(), "/api/v1/info")
    assert doc["objects_version"] == info["objects_version"] == objects_version()

    fresh = [_objects_version_in_a_new_process(seed) for seed in ("0", "1", "12345")]
    assert set(fresh) == {objects_version()}


def test_objects_version_changes_when_a_descriptor_changes():
    """Otherwise it is a constant dressed up as a fingerprint."""
    _, doc = _get(_api(), "/api/v1/objects")
    altered = json.loads(json.dumps(doc))
    del altered["objects_version"]
    altered["objects"][0]["name"] = "Something else"
    assert _fingerprint(altered) != doc["objects_version"]

    same = json.loads(json.dumps(doc))
    del same["objects_version"]
    assert _fingerprint(same) == doc["objects_version"]


def _fingerprint(doc):
    """The documented rule, recomputed here by hand: sha256 over the canonical
    JSON of the descriptor document with the fingerprint itself removed."""
    import hashlib
    body = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _objects_version_in_a_new_process(seed):
    code = "from ios2ha_camera.api import objects_version; print(objects_version())"
    env = {**os.environ, "PYTHONHASHSEED": seed}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env, check=True)
    return out.stdout.strip()
