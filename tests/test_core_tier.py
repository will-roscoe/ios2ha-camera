"""The "core tier": with both Home Assistant interfaces switched off, the
supervisor still starts, captures a real still off the phone, stacks it,
archives it to disk, and serves it back over HTTP. CONFIGURATION.md's Core
table promises exactly this -- everything else in that table is an addition
to it -- and nothing here should be a stand-in for a stand-in: the phone is a
real MJPEG server on loopback, the frames pass through a real Ingestor and
Upstream into a real RingBuffer, and the still that comes out is decoded and
checked as a real image.

The collaborators this tier does not need -- MQTT, the JSON API, D-Bus and
Bluetooth, ONVIF, the phone's cable channel -- are left however the service
itself represents "not present": the null MQTT client it already builds for
EXPOSE_MQTT=false, or simply never constructed, for everything that is an
optional constructor argument defaulting to None. None of them is a mock
standing in for a call this test expects to be answered.
"""

import asyncio
import io
import logging
import os
import threading
import time
from urllib.parse import urlparse

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
from tests.fakes.fake_camera import CREDENTIALS, FakeCamera

from ios2ha_camera import __main__ as main_module
from ios2ha_camera.archive import Archive
from ios2ha_camera.ingest import Ingestor
from ios2ha_camera.live import make_live_app
from ios2ha_camera.live_stack import LiveStacker
from ios2ha_camera.ring_buffer import RingBuffer
from ios2ha_camera.settings import Settings, both_interfaces_off_message
from ios2ha_camera.supervisor import Supervisor
from ios2ha_camera.upstream import Upstream

# The seven media paths CONFIGURATION.md's Core table names as served
# unconditionally -- the two flags under test gate the JSON API only. The
# screenshot route is among them even though this tier has no phone management
# to take one: it is advertised in MEDIA either way, so it answers 503 "no
# screenshot yet" rather than 404, and the advertised list stays true.
MEDIA_PATHS = {
    "/live", "/live/stacked", "/still.jpg",
    "/api/v1/stream/live", "/api/v1/stream/stacked", "/api/v1/snapshot/still",
    "/api/v1/snapshot/screenshot",
}


def _wait_for(pred, timeout=15.0, poll=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(poll)
    return False


def _archived_files(root):
    """Every still the archive has written under `root`, recursively -- the
    archive lays them out under year/month/day, so a fixed relative path
    would silently stop matching the moment a test crosses midnight."""
    found = []
    for dirpath, _, names in os.walk(root):
        found.extend(os.path.join(dirpath, n) for n in names if n.endswith(".jpg"))
    return found


def test_the_core_tier_captures_archives_and_serves_with_nothing_optional(tmp_path, caplog):
    s = Settings(
        expose_mqtt=False,
        expose_api=False,
        # Deliberately unparseable: with MQTT off, `_start_interfaces` still spins up
        # a background thread that builds a *real* MqttIO to clear retained discovery
        # topics. That must fail inside the constructor, before it ever opens a
        # socket, rather than reach for a broker this test never starts.
        mqtt_url="not://a valid url at all",
        stacking_mode="fixed", fixed_depth=10,
        archive_dir=str(tmp_path),
    )

    with FakeCamera(frames=100_000) as cam:
        u = urlparse(cam.url)
        s.phone_host, s.phone_port = u.hostname, u.port
        s.phone_user, s.phone_password = CREDENTIALS

        buf = RingBuffer(50_000_000)
        ing = Ingestor(s.phone_host, s.phone_port, s.phone_user, s.phone_password, buf)
        # No probe: it is Upstream's own feature, with its own tests, and a fixed-depth
        # capture does not need it to prove a still reaches the archive.
        #
        # `sup` does not exist yet -- it needs `up` first -- so `on_state` is a lambda
        # that looks it up at call time rather than a bound method, exactly as
        # __main__.py resolves the same chicken-and-egg problem.
        up = Upstream(ing, buf, s, on_state=lambda state: sup.on_stream_state(state),
                     probe=lambda: None)
        ing.on_state = up.on_ingest_state
        archive = Archive(str(tmp_path))

        mqtt = main_module._mqtt_for(s)  # NullMqttIO: the service's own "no broker" object
        sup = Supervisor(s, buf, mqtt, archive, upstream=up)
        # onvif, phone, starter, keyboard and opener are never constructed at all --
        # not one exists to be nulled out, which is the plainest way to prove they
        # are not needed. Their absence is what supervisor.py already documents as
        # "what happens when the D-Bus proxy or BlueZ is missing".
        stacker = LiveStacker(buf, s)
        app = make_live_app(buf, latest_still=lambda: sup.last_still_jpeg,
                            upstream=up, stacker=stacker)

        with caplog.at_level(logging.INFO):
            hub = main_module._start_interfaces(s, sup, app, mqtt)

        # No SSE hub, and the router carries only the seven media paths: the JSON
        # routes that EXPOSE_API would have added were never registered.
        assert hub is None
        paths = {r.resource.canonical for r in app.router.routes()}
        assert paths == MEDIA_PATHS
        # Per the addendum: at most this one line, never the rest of the log.
        main_lines = [r.getMessage() for r in caplog.records if r.name == main_module.__name__]
        assert both_interfaces_off_message() in main_lines

        stop = threading.Event()
        up.start()
        capture = threading.Thread(target=sup.run_capture, args=(stop,), daemon=True)
        capture.start()
        try:
            # Bypasses the (600 s default) interval wait without changing it --
            # exactly the manual-request path run_capture exists to serve, and the
            # only way to reach a real still without an equally real, needless wait.
            sup.capture_requested.set()
            assert _wait_for(lambda: _archived_files(tmp_path)), \
                "no still reached the archive within the timeout"
        finally:
            stop.set()
            sup.capture_requested.set()   # wake the loop so it can exit
            capture.join(timeout=5.0)
            up.stop(timeout=5.0)

    files = _archived_files(tmp_path)
    assert len(files) == 1, files   # the manual trigger fires exactly once
    with open(files[0], "rb") as f:
        data = f.read()
    assert len(data) > 1_000              # a stub-sized write would be a few bytes
    assert data[:3] == b"\xff\xd8\xff"   # real JPEG magic bytes, not just "some file"
    Image.open(io.BytesIO(data)).load()  # decodes as a real image

    async def _fetch_still():
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/still.jpg")
            return resp.status, await resp.read()

    status, body = asyncio.run(_fetch_still())
    assert status == 200
    # The route serves exactly what was archived: one capture, one still, and the
    # HTTP path that a Home Assistant camera entity actually calls.
    assert body == data
