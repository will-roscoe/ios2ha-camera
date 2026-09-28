"""The control page: a client of /api/v1, served only where the API is.

Everything Home Assistant can see and do with the camera, from a browser on the
LAN. It keeps no vocabulary of its own -- entities from /objects, values from
/events, writes through /control -- so these tests hold it to that: the only path
it may name is the one every client is told to hardcode.
"""
import asyncio
import re
import tomllib
from importlib import resources
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from ios2ha_camera.api import INFO, ROUTES, make_api
from ios2ha_camera.entities import MEDIA
from ios2ha_camera.settings import Settings

WEB = resources.files("ios2ha_camera") / "web"


def _fetch(app, *paths):
    """(status, headers, body) for each path, in one session: an aiohttp app is
    bound to the loop it first runs on."""
    async def run():
        out = []
        async with TestClient(TestServer(app)) as client:
            for path in paths:
                resp = await client.get(path, allow_redirects=False)
                out.append((resp.status, resp.headers, await resp.read()))
        return out
    got = asyncio.run(run())
    return got[0] if len(paths) == 1 else got


def test_the_page_is_served_with_the_api():
    status, headers, body = _fetch(make_api(dict, Settings()), "/ui/")
    assert status == 200
    assert headers["Content-Type"].startswith("text/html")
    assert b'<script src="app.js">' in body


def test_its_assets_are_served_with_their_types():
    got = _fetch(make_api(dict, Settings()), "/ui/app.js", "/ui/style.css")
    for (status, headers, _), kind in zip(got, ("javascript", "text/css"), strict=True):
        assert status == 200, kind
        assert kind in headers["Content-Type"], kind


def test_the_address_on_its_own_leads_to_the_page():
    status, headers, _ = _fetch(make_api(dict, Settings()), "/")
    assert status in (301, 302, 307, 308)
    assert headers["Location"] == "/ui/"


def test_there_is_no_page_without_the_api():
    """With EXPOSE_API off the API routes are never added, and neither is a page
    whose every request would fail."""
    (ui, _, _), (root, _, _) = _fetch(web.Application(), "/ui/", "/")
    assert ui == 404 and root == 404


def test_the_page_names_no_path_it_should_discover():
    """Only /api/v1/info is hardcoded; the rest comes from its routes map."""
    js = (WEB / "app.js").read_text()
    named = set(re.findall(r'["\'`](/api/v1/[^"\'`]*)["\'`]', js))
    assert named == {INFO}
    known = set(ROUTES.values()) | {m.path for m in MEDIA} | {INFO}
    assert named <= known


def test_the_page_never_parses_api_data_as_html():
    js = (WEB / "app.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in js, sink


def test_the_page_ships_in_the_package():
    """The image installs the package with pip; files not declared as package data
    are left behind, and the page would 404 in production only."""
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())
    data = pyproject["tool"]["setuptools"]["package-data"]["ios2ha_camera"]
    assert "web/*" in data
    for name in ("index.html", "app.js", "style.css"):
        assert (WEB / name).is_file(), name
