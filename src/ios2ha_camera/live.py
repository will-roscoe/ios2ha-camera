"""Re-serve the buffered MJPEG stream to Home Assistant and any other viewer.

The previous version of this system connected Home Assistant's MJPEG proxy
straight to the phone. One stalled client froze the phone's whole HTTP server
behind it, stalls piled up until the phone's kernel ran out of mbufs, and it
panicked -- ten days of timelapse lost before anyone noticed. This module
never touches the phone directly: it only reads `RingBuffer.latest()` (and,
for the stacked route, `LiveStacker.latest()`).

To make sure that failure can't repeat here, each client gets its own short
queue rather than a shared write loop. A single background task per source
polls it at a capped rate and fans the latest frame out to every registered
queue using non-blocking puts; if a client's queue is already full (it hasn't
drained fast enough), the oldest queued frame is dropped to make room for the
new one. That fan-out never awaits a client's socket, so a client whose write
is stalled can only stall its own request task -- never a source read, a
fan-out loop, or any other client.

Home Assistant is not the only viewer any more: `/live/stacked` serves a
noise-reduced feed from a `LiveStacker`, and both `/live` and `/live/stacked`
now hold the phone connection open (via `Upstream.hold`) only for as long as
they have a viewer attached -- that is what makes the connection on-demand.
It also means a stalled client is no longer free: its request task holds the
phone at about 300 mA for as long as the stall lasts, and aiohttp has no write
timeout to end it. So every write is bounded by `WRITE_TIMEOUT_S`, and a viewer
that stops reading for that long is treated exactly like one that left.

Holding and releasing a hold is non-blocking, and so is `LiveStacker.attach`/
`detach`, which is why a plain `with` and plain calls around the streaming
loop are safe here: nothing in this module may block the event loop, since
that would freeze every other viewer along with it.
"""

import asyncio
from contextlib import nullcontext

from aiohttp import web

from .entities import MEDIA

# Keyed by the same `id`s `make_live_app` builds handlers for ("live",
# "stacked", "still"), so a new API path always comes from the descriptor
# that advertises it rather than being typed a second time here.
_MEDIA_BY_ID = {m.id: m for m in MEDIA}

# Typed keys rather than plain strings: aiohttp warns on str keys, and an
# unattended service wants a log with nothing routine in it.
BUFFER = web.AppKey("buffer", object)
# The raw /live route's clients; kept for callers that inspect it directly.
CLIENTS = web.AppKey("clients", set)
SOURCES = web.AppKey("sources", list)
UPSTREAM = web.AppKey("upstream", object)
STACKER = web.AppKey("stacker", object)
BROADCAST_TASK = web.AppKey("broadcast_task", list)
LATEST_STILL = web.AppKey("latest_still", object)
LATEST_SCREENSHOT = web.AppKey("latest_screenshot", object)

BOUNDARY = "frame"
FPS_CAP = 12
FRAME_INTERVAL = 1.0 / FPS_CAP
QUEUE_SIZE = 2  # short: a client behind by more than this is dropped, not queued
VIEWER_LINGER_S = 30.0  # keep the phone connection open this long after the last viewer leaves
# A write stalled this long is a viewer that stopped reading, and its hold is dropped.
# 30 s rather than 5: at 5 s this also disconnected steady readers below roughly 1.2 Mbit/s
# (measured on loopback), which a Home Assistant viewer on a slow link can be, because the
# kernel only wakes a paused writer once much of the send buffer has drained. The cost of the
# longer wait is that a genuinely stalled viewer holds the phone for this plus the linger.
WRITE_TIMEOUT_S = 30.0


def _offer(queue, frame):
    """Put frame on queue without blocking, dropping the oldest entry if full."""
    try:
        queue.put_nowait(frame)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass  # another producer won the race; the frame is simply skipped


def _part(frame):
    return (
        b"--%b\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
        % (BOUNDARY.encode(), len(frame.data))
        + frame.data
        + b"\r\n"
    )


async def _broadcast(app, source, clients):
    """Fan the latest frame from one source out to its clients at a capped rate.

    Only ever touches the source's cheap latest() read and non-blocking queue
    ops, so a stalled client's full queue can never delay this loop -- for
    either the raw buffer or the stacker.
    """
    last_ts = None
    try:
        while True:
            frame = source()
            if frame is not None and frame.ts != last_ts:
                last_ts = frame.ts
                for queue in list(clients):
                    _offer(queue, frame)
            await asyncio.sleep(FRAME_INTERVAL)
    except asyncio.CancelledError:
        pass


def _stream_handler(clients, source, attach=None, detach=None):
    """Build a streaming handler fed by one source (the raw buffer or the stacker).

    A connected viewer holds the upstream phone connection open for as long as
    it stays connected, via `Upstream.hold` -- released (after `VIEWER_LINGER_S`,
    so a quick reconnect doesn't re-trigger a probe) once it goes away. `attach`/
    `detach`, when given, additionally start/stop the stacker's background
    thread around the same lifetime; the raw route passes neither, since it
    has nothing of its own to start.
    """
    async def handler(request):
        app = request.app
        upstream = app.get(UPSTREAM)
        hold = upstream.hold("viewer", linger_s=VIEWER_LINGER_S) if upstream else nullcontext()
        attached = False
        try:
            with hold:
                # Upstream.hold() has already taken effect by the time it
                # returns above -- it mutates its counts at call time, not at
                # __enter__ -- so attach() must run inside this block, not
                # before it: if attach() raises (thread-start failure under
                # exhaustion, say), unwinding through `with hold`'s __exit__
                # is the only thing that still releases it. Calling it before
                # entering `with hold` would leak the phone connection
                # forever, with no linger timeout to fall back on since
                # release() would never even be reached.
                if attach is not None:
                    attach()
                    attached = True
                resp = web.StreamResponse(headers={
                    "Content-Type": "multipart/x-mixed-replace; boundary=%s" % BOUNDARY,
                    "Cache-Control": "no-cache",
                })
                await resp.prepare(request)

                queue = asyncio.Queue(maxsize=QUEUE_SIZE)
                clients.add(queue)
                # Seed with whatever is already available so a viewer sees a
                # frame immediately instead of waiting for the next broadcast tick.
                latest = source()
                if latest is not None:
                    _offer(queue, latest)
                try:
                    while True:
                        frame = await queue.get()
                        # Bounded because this viewer holds the phone connection:
                        # aiohttp has no write timeout, so a client that stays
                        # connected but stops reading would hold it for good.
                        await asyncio.wait_for(resp.write(_part(frame)), WRITE_TIMEOUT_S)
                except (TimeoutError, ConnectionError, asyncio.CancelledError):
                    # ConnectionError, not just its Reset/Aborted subclasses: aiohttp raises
                    # a bare ConnectionError("Connection lost") when a viewer goes away
                    # mid-write, and Home Assistant's camera does exactly that every time it
                    # grabs a thumbnail. A client leaving is normal and must not put a
                    # traceback in an unattended service's log. A write that timed out is
                    # a client that stopped reading, and is treated as one that left.
                    pass
                finally:
                    clients.discard(queue)
                return resp
        finally:
            # Guarded by attached, not just `detach is not None`: if attach()
            # itself raised, there is no attach to undo, and calling detach()
            # anyway would decrement another viewer's count instead of this
            # (nonexistent) one.
            if attached and detach is not None:
                detach()
    return handler


def _reject_undeclared_params(handler, accepted):
    """Wrap `handler` so a query parameter its descriptor does not declare is a
    400 naming what is accepted, rather than a silently ignored knob -- silently
    ignoring it looks exactly like a bug: a caller who mistypes `?width=640` on
    a route that cannot resize should see the 400 immediately, not get back a
    full-resolution stream and assume it worked.

    Only ever applied to the new `/api/v1/...` paths. The old paths
    (`/live`, `/live/stacked`, `/still.jpg`) are the Generic Camera
    integration's permanent public interface, and a user's existing entry may
    already carry a cache-busting query string that must keep working, so the
    old paths register `handler` directly, unwrapped.
    """
    async def wrapped(request):
        extra = sorted(set(request.query.keys()) - accepted)
        if extra:
            # The envelope every other /api/v1 refusal uses, so a client needs
            # one error parser -- and this is the route most likely to be sent
            # a stray query string.
            return web.json_response(
                {"ok": False,
                 "error": f"unexpected query parameter(s): {extra}; "
                          f"accepted: {sorted(accepted)}",
                 "accepted": sorted(accepted)},
                status=400, headers={"Cache-Control": "no-store"})
        return await handler(request)
    return wrapped


async def _still(request):
    """The latest stacked still, as one JPEG.

    This is what a viewer should see as the camera's thumbnail: the stacked,
    noise-reduced image rather than a single raw frame, which at night is
    mostly noise. It is served from memory and never touches the phone -- no
    hold is taken here at all -- so a dashboard refreshing its thumbnail costs
    the phone nothing, which is also what makes the on-demand upstream
    connection possible for the streaming routes.

    Before the first still of the run, it falls back to the newest buffered
    frame so the thumbnail is never blank.
    """
    provider = request.app.get(LATEST_STILL)
    jpeg = provider() if provider is not None else None
    if jpeg is None:
        frame = request.app[BUFFER].latest()
        jpeg = frame.data if frame is not None else None
    if jpeg is None:
        raise web.HTTPServiceUnavailable(text="no frame yet")
    return web.Response(body=jpeg, content_type="image/jpeg",
                        headers={"Cache-Control": "no-cache"})


async def _screenshot(request):
    """The phone's screen, as the screenshot button last captured it.

    Served from memory like /still.jpg, so a camera thumbnail costs the phone
    nothing. There is deliberately no fallback: before the first press this is
    a 503, because a camera frame is not a screenshot and showing one would
    answer a different question than the one asked.
    """
    provider = request.app.get(LATEST_SCREENSHOT)
    jpeg = provider() if provider is not None else None
    if jpeg is None:
        raise web.HTTPServiceUnavailable(text="no screenshot yet")
    return web.Response(body=jpeg, content_type="image/jpeg",
                        headers={"Cache-Control": "no-cache"})


async def _start_broadcast(app):
    app[BROADCAST_TASK] = [asyncio.ensure_future(_broadcast(app, source, clients))
                           for source, clients in app[SOURCES]]


async def _stop_broadcast(app):
    tasks = app.get(BROADCAST_TASK) or []
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def make_live_app(buffer, latest_still=None, upstream=None, stacker=None,
                  latest_screenshot=None):
    """`latest_still` is a callable returning the most recent stacked still as
    JPEG bytes, or None before the first one; it backs the /still.jpg route.

    `upstream`, if given, is held open (see `Upstream.hold`) for as long as any
    viewer is connected to /live or /live/stacked -- this is what lets the
    phone connection be opened on demand rather than for the life of the
    process. `stacker`, if given, additionally serves /live/stacked from its
    noise-reduced feed and has its background thread attached/detached to the
    lifetime of that route's viewers.

    Each of these three routes is also registered under its `entities.MEDIA`
    path (`/api/v1/stream/live`, `/api/v1/stream/stacked`,
    `/api/v1/snapshot/still`) -- the same handler object, not a redirect, so a
    302 mid-MJPEG never has to survive Home Assistant's camera proxy or a
    Cloudflare tunnel. The old paths are the Generic Camera integration's
    permanent public interface and keep accepting any query string; only the
    new paths refuse an undeclared parameter with a 400, via
    `_reject_undeclared_params`.

    `latest_screenshot` is the same arrangement for the phone's screen: a
    callable returning the most recent screenshot as PNG bytes, or None before
    the screenshot button has been pressed, backing
    `/api/v1/snapshot/screenshot`. It has no older path to keep working, so it
    is registered under its `entities.MEDIA` path only.
    """
    app = web.Application()
    app[BUFFER] = buffer
    app[LATEST_STILL] = latest_still
    app[LATEST_SCREENSHOT] = latest_screenshot
    app[UPSTREAM] = upstream
    app[STACKER] = stacker
    app[SOURCES] = []

    raw_clients = set()
    app[CLIENTS] = raw_clients
    app[SOURCES].append((buffer.latest, raw_clients))
    raw_handler = _stream_handler(raw_clients, buffer.latest)
    app.router.add_get("/live", raw_handler)
    app.router.add_get(_MEDIA_BY_ID["live"].path,
                       _reject_undeclared_params(raw_handler, set(_MEDIA_BY_ID["live"].params)))

    if stacker is not None:
        stacked_clients = set()
        app[SOURCES].append((stacker.latest, stacked_clients))
        stacked_handler = _stream_handler(
            stacked_clients, stacker.latest, attach=stacker.attach, detach=stacker.detach)
        app.router.add_get("/live/stacked", stacked_handler)
        app.router.add_get(
            _MEDIA_BY_ID["stacked"].path,
            _reject_undeclared_params(stacked_handler, set(_MEDIA_BY_ID["stacked"].params)))

    app.router.add_get("/still.jpg", _still)
    app.router.add_get(_MEDIA_BY_ID["still"].path,
                       _reject_undeclared_params(_still, set(_MEDIA_BY_ID["still"].params)))
    app.router.add_get(
        _MEDIA_BY_ID["screenshot"].path,
        _reject_undeclared_params(_screenshot, set(_MEDIA_BY_ID["screenshot"].params)))
    app.on_startup.append(_start_broadcast)
    app.on_cleanup.append(_stop_broadcast)
    return app
