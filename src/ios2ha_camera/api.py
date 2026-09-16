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
  `Supervisor`: it is handed `state()` and `on_control()`. That is what keeps
  this module testable against a plain dict, and what stops it growing a second
  opinion about what the state is or a second control vocabulary to keep in step
  with the first.

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
# hardcode. `events` has no handler registered yet, but is named here anyway: a
# client discovers every path from this map, so `events`'s path has to be fixed
# now rather than sprung on it once the handler exists. Until then it 404s,
# which a client can read and retry later, where a client that never learns the
# name cannot ask at all.
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

# The entire control vocabulary: the numbers, selects and switches that carry a
# value, and the buttons that carry only a press. Keyed by object id, which is
# unambiguous here even though it is not across the whole registry -- `screenshot`
# is both a camera and a button, and only the button is writable.
_WRITABLE = {e.object_id: e for e in ENTITIES if e.writable}


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


async def _posted_value(request):
    """The `value` from the request body, or None when there is no body.

    A button press legitimately carries nothing, so an empty body is not an
    error here; it is the value controls that go on to refuse a missing value.
    Anything that is neither empty nor a JSON object with a `value` key raises,
    because guessing what was meant is how a typo becomes a silent no-op.
    """
    raw = (await request.text()).strip()
    if not raw:
        return None
    try:
        body = json.loads(raw)
    except ValueError as err:
        raise ValueError(f"body is not JSON: {err}") from err
    if not isinstance(body, dict) or "value" not in body:
        raise ValueError('body must be a JSON object with a "value" key')
    return body["value"]


def _unacceptable(e, value):
    """Why the registry refuses this value, or None if it does not.

    Only what the registry itself declares: a select's options and a number's
    range. Type coercion is `Settings.apply_control`'s business -- the boolean
    spellings a switch accepts are stated there, and a copy of that table here
    would be the second vocabulary this route exists to avoid. So a switch is
    validated by nothing and passed through as sent.

    Rejecting here rather than downstream is what makes a bad write visible:
    `Supervisor.handle_control` is called from paho's network thread, swallows
    everything by design, and so can never tell a caller that it refused.
    """
    if value is None:
        return f'{e.object_id} needs a value: POST {{"value": ...}}'
    if e.options is not None:
        if value not in e.options:
            return f"{e.object_id} must be one of {list(e.options)}, not {value!r}"
    elif e.lo is not None:
        # bool is an int in Python, and True would land inside most of these
        # ranges. A switch's value arriving on a number is a caller bug.
        if isinstance(value, bool) or not isinstance(value, int | float):
            return f"{e.object_id} must be a number between {e.lo} and {e.hi}, not {value!r}"
        if not e.lo <= value <= e.hi:
            return f"{e.object_id} must be between {e.lo} and {e.hi}, not {value!r}"
    return None


def _refused(status, error, **extra):
    """A refusal a person can act on: why, and enough to fix the call.

    `ok` is the one key every control response carries, so a client can branch on
    it without first knowing which failures are possible.
    """
    return web.json_response({"ok": False, "error": error, **extra},
                             status=status, headers=_NO_STORE)


def add_api_routes(app, state, settings, on_control=lambda n, v: None):
    """Register the API on an application the caller owns.

    `state` is called per request and returns the current values keyed by object
    id; `on_control` is handed an accepted write, and is
    `Supervisor.handle_control` in the live service. Both are callables rather
    than the supervisor itself, which is what keeps this module independently
    testable and free of a second opinion about the state.

    `settings` is not read yet. It is in the signature now because optional auth
    (deliberately deferred, not forgotten) lands on this surface: adding it
    later must not change how two other modules call this.
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

    async def post_control(request):
        """One verb over the whole vocabulary: every switch, number, select and
        button, mapping straight onto the existing control path.

        200 means accepted and dispatched, not applied: `handle_control` is
        fire-and-forget by necessity, so a caller that wants confirmation reads
        `/state` (or, later, the event stream) rather than this response.
        """
        name = request.match_info["name"]
        e = _WRITABLE.get(name)
        if e is None:
            # Named separately because they are different mistakes: a typo, and
            # a real entity that was never meant to be written.
            declared = any(x.object_id == name for x in ENTITIES)
            return _refused(404, f"{name} is read-only" if declared
                           else f"there is no control named {name}",
                           name=name, accepted=sorted(_WRITABLE))
        try:
            value = await _posted_value(request)
        except ValueError as err:
            return _refused(400, str(err), name=name)
        if e.domain == "button":
            # A press carries no value. Home Assistant's own button sends
            # "PRESS" and a script may send anything; none of it means a thing
            # to a press, so it is dropped here rather than passed on as if the
            # supervisor might read it.
            value = None
        else:
            problem = _unacceptable(e, value)
            if problem:
                log.warning("refused control %s: %s", name, problem)
                # The real current value, so a client that optimistically
                # displayed the rejected one can put it straight back. Exactly
                # what _apply_setting republishes over MQTT for the same case.
                return _refused(400, problem, name=name,
                               current=state().get(e.state_key))
        on_control(name, value)
        return web.json_response({"ok": True, "name": name, "value": value},
                                 headers=_NO_STORE)

    app.router.add_get(INFO, get_info)
    app.router.add_get(ROUTES["objects"], get_objects)
    app.router.add_get(ROUTES["media"], get_media)
    app.router.add_get(ROUTES["state"], get_state)
    app.router.add_post(ROUTES["control"], post_control)
    return app


def make_api(state, settings, on_control=lambda n, v: None):
    """A standalone application serving the API, for tests and for a deployment
    that wants it on its own port. The live service calls `add_api_routes` on the
    application already serving the media routes instead."""
    return add_api_routes(web.Application(), state, settings, on_control=on_control)
