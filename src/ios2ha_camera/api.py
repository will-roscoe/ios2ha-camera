"""The JSON API, and the second renderer over `entities.ENTITIES`.

`mqtt_io` renders the registry as Home Assistant discovery payloads. This module
renders the same declaration as JSON for a Home Assistant custom component that
lives in its own repository, on its own release cycle. That release cycle is the
whole reason the shapes here are worth care: the component ships against what
this module serves, and a key that stops being served breaks a repository this
one cannot see and cannot fix.

Three properties follow from that, and each is pinned by a test:

- **`/api/v1/info` is the only path a client may hardcode.** It names every other
  route, so a moved endpoint, a new media format or a future WebRTC stream is a
  change to a served document rather than a component release. A key missing
  from `info` is a path someone is then forced to hardcode.
- **Every descriptor carries every key**, null where the field does not apply.
  A client that must test for a key before reading it grows a different code
  path per domain, and the domain it has not met yet is the one it gets wrong.
- **The state is a callable, not the supervisor.** `api` never imports
  `Supervisor`: it is handed `state()`. That is what keeps this module testable
  against a plain dict, and what stops it growing a second opinion about what
  the state is.

Routes are registered at absolute paths on an application the caller owns,
rather than as a sub-application mounted under a prefix. The media routes
(`/api/v1/stream/*`, `/api/v1/snapshot/*`) are served by `live.py` on that same
application, and a prefixed sub-app sitting across that namespace is the kind of
routing interaction that behaves in a test and surprises someone at 3am.
"""

import hashlib
import json
import logging

from aiohttp import web

from . import version
from .entities import ENTITIES, MEDIA, device_info

log = logging.getLogger(__name__)

# Per-interface version integers, deliberately not the package version: the two
# paths break independently, so restructuring MQTT topics must not tell every
# API client it may be incompatible, and vice versa.
API_INTERFACE = 1
MQTT_INTERFACE = 1

PREFIX = "/api/v1"
INFO = PREFIX + "/info"

# Every route except `info` itself, which is the one a client is told to
# hardcode. `events` is served by a later task; it is named here because its
# path is already decided and a client that discovers it before it exists gets a
# 404 it can read, where a client that never learns the name cannot ask at all.
ROUTES = {
    "objects": PREFIX + "/objects",
    "state": PREFIX + "/state",
    "events": PREFIX + "/events",
    "control": PREFIX + "/control/{name}",
    "media": PREFIX + "/media",
}

# Nothing served here is cacheable: `/state` is live, and a stale `/objects` or
# `/info` held by a proxy after a restart is a component building entities that
# no longer exist. Set explicitly because the deployment is reached through a
# Cloudflare tunnel, which is free to apply its own heuristics to a response
# that does not say. Same reasoning as the still route in live.py.
_NO_STORE = {"Cache-Control": "no-store"}


def _object(e):
    """One entity as the component sees it.

    Deliberately not the whole record: `topic` is where MQTT publishes a
    camera's payload and means nothing to an HTTP client, and leaking it would
    make the MQTT topic layout part of a second published interface.

    `object_id` alone is not an identity -- `screenshot` is both a camera and a
    button -- so the pair (`domain`, `object_id`) is what a client should key on.
    """
    return {"object_id": e.object_id, "domain": e.domain, "name": e.name,
            "state_key": e.state_key, "device_class": e.device_class,
            "unit": e.unit, "state_class": e.state_class,
            "options": list(e.options) if e.options is not None else None,
            "lo": e.lo, "hi": e.hi, "step": e.step, "category": e.category,
            "icon": e.icon, "writable": e.writable, "precision": e.precision}


def _media(m):
    """One media source. `path` is served as `url` because that is the name the
    component reads, and `holds_phone` is the field that justifies describing
    media at all: it is capability plus cost."""
    return {"id": m.id, "kind": m.kind, "formats": list(m.formats), "url": m.path,
            "holds_phone": m.holds_phone, "params": dict(m.params)}


def descriptors():
    """Everything a component needs to build its entities, in declaration order."""
    return {"objects": [_object(e) for e in ENTITIES],
            "media": [_media(m) for m in MEDIA]}


def _fingerprint(doc):
    """A content hash of the descriptor document, stable across processes.

    Not `hash()` of anything: that is seeded per run, so every reconnect would
    look like a changed registry. Canonical JSON (sorted keys, no whitespace)
    means the fingerprint tracks what is actually served rather than dictionary
    ordering, and is reproducible by the client if it ever wants to check.

    Named with its algorithm so a future change to it is legible on the wire
    rather than looking like a registry that changed.
    """
    body = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(body).hexdigest()


def objects_version():
    """The fingerprint of the current descriptors.

    Carried by `/info` so a reconnecting client can tell whether its cached
    `/objects` is still good without refetching 86 descriptors to find out.
    """
    return _fingerprint(descriptors())


def info():
    """The discovery document: versions, the device, and where everything is."""
    return {"version": version.VERSION,
            "interfaces": {"api": API_INTERFACE, "mqtt": MQTT_INTERFACE},
            "objects_version": objects_version(),
            # The same block MQTT publishes inside every discovery payload,
            # from the same declaration; see entities.device_info().
            "device": device_info(),
            "routes": dict(ROUTES)}


def add_api_routes(app, state, settings):
    """Register the API on an application the caller owns.

    `state` is called per request and returns the current values keyed by object
    id. A callable rather than the supervisor itself, which is what keeps this
    module independently testable and free of a second opinion about the state.

    `settings` is not read yet. It is in the signature because the wiring task
    passes it and because optional auth (deliberately deferred, not forgotten)
    lands on this surface: adding it later must not change how two other modules
    call this.
    """
    async def get_info(request):
        return web.json_response(info(), headers=_NO_STORE)

    async def get_objects(request):
        doc = descriptors()
        # The fingerprint covers the rest of the document, so it is added after
        # it is computed rather than being hashed along with itself.
        return web.json_response({"objects_version": _fingerprint(doc), **doc},
                                 headers=_NO_STORE)

    async def get_media(request):
        return web.json_response({"media": [_media(m) for m in MEDIA]},
                                 headers=_NO_STORE)

    async def get_state(request):
        return web.json_response(state(), headers=_NO_STORE)

    app.router.add_get(INFO, get_info)
    app.router.add_get(ROUTES["objects"], get_objects)
    app.router.add_get(ROUTES["media"], get_media)
    app.router.add_get(ROUTES["state"], get_state)
    return app


def make_api(state, settings):
    """A standalone application serving the API, for tests and for a deployment
    that wants it on its own port. The live service calls `add_api_routes` on the
    application already serving the media routes instead."""
    return add_api_routes(web.Application(), state, settings)
