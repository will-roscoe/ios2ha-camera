"""Built timelapses are served beside the other media, with Range so players can seek.

Served by the media application like /still.jpg, so they are there with the JSON API
switched off -- an MQTT user reads the catalogue's urls and fetches them here.
"""
import asyncio

from aiohttp.test_utils import TestClient, TestServer

from ios2ha_camera.live import make_live_app
from ios2ha_camera.ring_buffer import RingBuffer

BODY = bytes(range(256)) * 4


def _gets(app, *paths, headers=None):
    """Several GETs in one loop: an aiohttp app binds to the first loop it serves."""
    async def run():
        out = []
        async with TestClient(TestServer(app)) as client:
            for path in paths:
                resp = await client.get(path, headers=headers or {})
                out.append((resp.status, resp.headers, await resp.read()))
        return out
    return asyncio.run(run())


def _get(app, path, headers=None):
    return _gets(app, path, headers=headers)[0]


def _app(tmp_path, with_dir=True):
    d = tmp_path / "timelapse"
    d.mkdir()
    (d / "last_7d.mp4").write_bytes(BODY)
    return make_live_app(RingBuffer(10_000), timelapse_dir=str(d) if with_dir else None)


def test_a_timelapse_is_served_as_video(tmp_path):
    status, headers, body = _get(_app(tmp_path), "/api/v1/timelapse/last_7d.mp4")
    assert status == 200 and body == BODY
    assert headers["Content-Type"] == "video/mp4"
    assert headers["Cache-Control"] == "no-cache"


def test_a_player_can_ask_for_part_of_it(tmp_path):
    status, headers, body = _get(_app(tmp_path), "/api/v1/timelapse/last_7d.mp4",
                                 {"Range": "bytes=0-9"})
    assert status == 206 and body == BODY[:10]
    assert headers["Content-Range"] == f"bytes 0-9/{len(BODY)}"


def test_only_names_in_the_folder_are_served(tmp_path):
    app = _app(tmp_path)
    (tmp_path / "secret.mp4").write_bytes(b"x")
    paths = ("/api/v1/timelapse/missing.mp4", "/api/v1/timelapse/..%2Fsecret.mp4",
             "/api/v1/timelapse/LAST_7D.mp4")
    assert [status for status, _, _ in _gets(app, *paths)] == [404, 404, 404]


def test_switched_off_is_not_found(tmp_path):
    assert _get(_app(tmp_path, with_dir=False), "/api/v1/timelapse/last_7d.mp4")[0] == 404


def test_an_undeclared_query_is_refused_like_the_other_media(tmp_path):
    assert _get(_app(tmp_path), "/api/v1/timelapse/last_7d.mp4?width=640")[0] == 400
