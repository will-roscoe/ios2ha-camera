"""Push state changes to API clients over Server-Sent Events.

A Home Assistant custom component holds one of these streams and drives every
entity from it with no polling, so the stream is its whole view of the camera.
That sets the priorities here, in order:

1. **A client never holds a wrong value without knowing it.** Unlike a dropped
   MJPEG frame, which the next frame replaces, a dropped state delta leaves the
   client's copy wrong until that key changes again, which for a setting may be
   never. So a client that cannot keep up is disconnected, never thinned: it
   reconnects and gets a fresh snapshot, the same recovery MQTT's retained
   messages give.
2. **Nothing a client does can stall a publisher or another client.** State is
   published from the health, ingest and device threads and from paho's network
   thread, on paths where a stall freezes the camera. `EventHub.publish` only
   encodes and hands off to the event loop; fan-out on the loop only makes
   non-blocking queue puts; each client's socket is written by its own request
   task, with a bounded write, exactly as `live.py` does for MJPEG viewers.
3. **Nothing is left behind.** Every way a stream ends -- the client leaving, a
   write timing out, a full queue, the server shutting down -- goes through the
   same `finally` that forgets the client.

Wire format, standard SSE, one JSON object per event:

    event: snapshot                 once, first, on connect
    data: {"objects_version": "sha256:...", "state": {...every key...}}

    event: state                    only the keys whose served value changed
    data: {"state": {"phone_battery": 80}}

    event: objects_changed          the descriptors a client cached are stale
    data: {"objects_version": "sha256:..."}

    : keepalive                     a comment, while nothing else is said

An events subscriber takes no `Upstream.hold`, unlike a `/live` viewer: state is
cheap, and keeping the phone's radio up because a dashboard is watching a sensor
would drain its battery for nothing.
"""

import asyncio
import json
import logging

from aiohttp import web

log = logging.getLogger(__name__)

# Well under the ~100 s after which a Cloudflare tunnel closes an HTTP connection
# that has sent nothing; the state can be quiet for much longer than that. Also
# how long a client that vanished while the stream was idle goes unnoticed:
# aiohttp does not wake a handler when its connection drops, so it is the next
# write that finds out.
KEEPALIVE_S = 20.0
# aiohttp has no write timeout of its own, so a client that stays connected but
# stops reading would otherwise park its request task for good. Events are small,
# so a reader that is merely slow never comes near this.
WRITE_TIMEOUT_S = 30.0
# Deltas a client may fall behind by, beyond what its socket buffers already
# hold, before it is disconnected. Generous: the health loop publishes every 30 s
# and a burst is a few dozen events, so only a client that has stopped reading
# fills it.
QUEUE_SIZE = 256

_KEEPALIVE = b": keepalive\n\n"
_CLOSE = object()  # queued to end a stream cleanly at shutdown


def _event(name, payload):
    # json.dumps never emits a raw newline (it escapes them inside strings), so
    # the whole payload is one `data:` line, which is what SSE requires.
    body = json.dumps(payload, separators=(",", ":"))
    return f"event: {name}\ndata: {body}\n\n".encode()


def _served(d):
    """Each value as it will be served: `{key: (canonical_json, value)}`.

    Changes are judged on the canonical JSON rather than on Python equality,
    because that is what a client was actually told: `0 == False` and
    `1 == 1.0` in Python, but a client sent `false` and now owed `0` has been
    told something different. A tuple and a list are the same JSON and are not
    a change. The value kept is the JSON round-trip, so what is compared is
    exactly what is later sent, and a caller that mutates its dict afterwards
    cannot change what was published.

    A value that cannot be encoded is logged and left out rather than raised:
    this runs on publisher threads that must never fail because of a client
    interface.
    """
    out = {}
    for key, value in d.items():
        try:
            text = json.dumps(value, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            log.exception("state %s is not JSON-encodable; not sent to event clients", key)
            continue
        out[key] = (text, json.loads(text))
    return out


class _Client:
    __slots__ = ("queue", "transport", "gone", "writing")

    def __init__(self, queue_size, transport):
        self.queue = asyncio.Queue(maxsize=queue_size)
        self.transport = transport
        self.gone = False
        self.writing = False

    def abort(self):
        """End the connection now. Not `close()`: that waits to flush the send
        buffer first, which for a client that stopped reading is never."""
        self.gone = True
        if self.transport is not None:
            self.transport.abort()


class EventHub:
    """Fan published state out to every connected `/api/v1/events` client.

    Feed it with `publish(d)` from any thread, after the value `d` carries is
    already visible to the `state` callable the route serves snapshots from.
    `add_api_routes` registers the route and ties the hub to the application's
    event loop; until that loop is running a publish is simply not delivered,
    since there is nobody to deliver it to and the first snapshot covers it.

    The intervals and the queue bound are injectable so tests need not wait.
    """

    def __init__(self, *, keepalive_s=KEEPALIVE_S, write_timeout_s=WRITE_TIMEOUT_S,
                 queue_size=QUEUE_SIZE):
        if queue_size < 1:
            raise ValueError("queue_size must be at least 1: the snapshot needs a slot")
        self._keepalive_s = keepalive_s
        self._write_timeout_s = write_timeout_s
        self._queue_size = queue_size
        self._clients = set()
        # What every connected client has been told, per key, as `_served` pairs.
        # Only ever touched on the event loop.
        self._told = {}
        self._objects_version = None
        # Written only on the loop; read from publisher threads, where a plain
        # attribute read is atomic and a stale None just means "not yet".
        self._loop = None
        self._closing = False

    @property
    def client_count(self):
        return len(self._clients)

    # -- any thread ---------------------------------------------------------

    def publish(self, d):
        """Send whatever in `d` differs from what clients were last told.

        Callable from any thread and never blocks: it encodes on the caller's
        thread and hands the rest to the event loop. `d` need not be a delta --
        republishing a whole unchanged dict emits nothing -- but the state it
        carries must already be readable through the route's `state` callable
        when this is called, or a client connecting in between could miss it.
        """
        loop = self._loop
        if loop is None or not d:
            return
        served = _served(d)
        if not served:
            return
        try:
            loop.call_soon_threadsafe(self._apply_state, served)
        except RuntimeError:
            pass  # the loop closed after it was read above; nobody is listening

    def set_objects_version(self, version):
        """Record the descriptors' fingerprint; tell clients if it changed.

        Carried in every snapshot, so a client reconnecting after a restart
        with a different registry can tell that its cached `/objects` is stale.
        Within one process the registry never changes, so in the service this
        is set once, by `add_api_routes`.
        """
        loop = self._loop
        if loop is None:
            self._objects_version = version
            return
        try:
            loop.call_soon_threadsafe(self._apply_objects_version, version)
        except RuntimeError:
            self._objects_version = version

    # -- the event loop -----------------------------------------------------

    def _apply_state(self, served):
        changed = {k: pair for k, pair in served.items()
                   if k not in self._told or self._told[k][0] != pair[0]}
        if not changed:
            return
        self._told.update(changed)
        self._fan_out(_event("state", {"state": {k: v for k, (_, v) in changed.items()}}))

    def _apply_objects_version(self, version):
        if version == self._objects_version:
            return
        self._objects_version = version
        self._fan_out(_event("objects_changed", {"objects_version": version}))

    def _fan_out(self, chunk):
        for client in list(self._clients):
            try:
                client.queue.put_nowait(chunk)
            except asyncio.QueueFull:
                # Its request task is stuck in a write, or it could not have
                # fallen this far behind. Skipping the event would leave it
                # silently wrong, so it is disconnected instead, and gets a
                # fresh snapshot when it reconnects.
                log.info("event client fell %d events behind; disconnecting it",
                         self._queue_size)
                self._clients.discard(client)
                client.abort()

    def _subscribe(self, request, state):
        """Register a client with its snapshot already queued.

        Everything here happens in one event-loop step, with no `await`, and
        that is what makes snapshot-then-deltas lose nothing. Deltas reach
        `_told` and the client queues only through callbacks on this same loop,
        so each one is either applied before this step, and so is in the
        snapshot, or runs after it, and so is queued behind the snapshot for
        this client. A delta published while the snapshot was being read can
        therefore arrive after a snapshot that already contains it, which is
        harmless: applying a delta twice leaves the same state.

        The snapshot is `_told`, not `state()` directly, so that every client
        holds the same view and the change test in `_apply_state` is always
        against what each client actually has. `state()` is read fresh and
        folded in first -- as a publish, so clients already connected hear
        about anything it moved -- because it can be newer than anything
        published: it may include values computed on demand.
        """
        self._apply_state(_served(state()))
        client = _Client(self._queue_size, request.transport)
        self._clients.add(client)
        client.queue.put_nowait(_event("snapshot", {
            "objects_version": self._objects_version,
            "state": {k: v for k, (_, v) in self._told.items()}}))
        return client

    async def serve(self, request, state):
        """The `/api/v1/events` handler body; `state` is the route's snapshot source."""
        if self._closing or self._loop is None:
            raise web.HTTPServiceUnavailable(text="shutting down")
        client = self._subscribe(request, state)
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "Cache-Control": "no-store"})
        try:
            await resp.prepare(request)
            while not client.gone:
                try:
                    item = await asyncio.wait_for(client.queue.get(), self._keepalive_s)
                except TimeoutError:
                    item = _KEEPALIVE
                # Send everything already waiting in one write, so a client
                # catching up after a burst costs one bounded write, not many.
                chunks = []
                while item is not _CLOSE:
                    chunks.append(item)
                    try:
                        item = client.queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                if chunks:
                    client.writing = True
                    try:
                        await asyncio.wait_for(resp.write(b"".join(chunks)),
                                               self._write_timeout_s)
                    finally:
                        client.writing = False
                if item is _CLOSE:
                    break
        except TimeoutError:
            # A client that stopped reading, treated as one that left. The write
            # was cut off mid-event, so the connection is ended outright rather
            # than finished with a truncated event on it.
            client.abort()
        except ConnectionError:
            # Normal: a client that leaves (aiohttp raises a bare
            # ConnectionError("Connection lost")), or one aborted above for
            # falling behind. Neither belongs in an unattended service's log.
            pass
        finally:
            self._clients.discard(client)
        return resp

    # -- application lifecycle ---------------------------------------------

    def attach(self, app):
        """Tie the hub to `app`'s event loop for as long as `app` runs."""
        app.on_startup.append(self._on_startup)
        app.on_shutdown.append(self._on_shutdown)
        app.on_cleanup.append(self._on_cleanup)

    async def _on_startup(self, app):
        self._closing = False
        self._loop = asyncio.get_running_loop()

    async def _on_shutdown(self, app):
        # aiohttp runs this before it waits for handlers to finish, and an idle
        # stream would otherwise wait out its keepalive. A client mid-write may
        # be stalled on its socket, so it is ended outright; the rest are told
        # to finish, which ends their response cleanly.
        self._closing = True
        for client in list(self._clients):
            if client.writing:
                self._clients.discard(client)
                client.abort()
                continue
            try:
                client.queue.put_nowait(_CLOSE)
            except asyncio.QueueFull:
                self._clients.discard(client)
                client.abort()

    async def _on_cleanup(self, app):
        self._loop = None
