"""Re-serve the buffered MJPEG stream to Home Assistant and any other viewer.

The previous version of this system connected Home Assistant's MJPEG proxy
straight to the phone. One stalled client froze the phone's whole HTTP server
behind it, stalls piled up until the phone's kernel ran out of mbufs, and it
panicked -- ten days of timelapse lost before anyone noticed. This module
never touches the phone: it only reads `RingBuffer.latest()`.

To make sure that failure can't repeat here, each client gets its own short
queue rather than a shared write loop. A single background task polls the
ring buffer at a capped rate and fans the latest frame out to every
registered queue using non-blocking puts; if a client's queue is already
full (it hasn't drained fast enough), the oldest queued frame is dropped to
make room for the new one. That fan-out never awaits a client's socket, so a
client whose write is genuinely stalled can only stall its own request task
-- never the ring buffer read, the fan-out loop, or any other client.
"""

import asyncio
from aiohttp import web

BOUNDARY = "frame"
FPS_CAP = 12
FRAME_INTERVAL = 1.0 / FPS_CAP
QUEUE_SIZE = 2  # short: a client behind by more than this is dropped, not queued


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


async def _broadcast(app):
    """Fan the latest frame out to every connected client at a capped rate.

    Only ever touches the buffer's cheap latest() read and non-blocking queue
    ops, so a stalled client's full queue can never delay this loop.
    """
    buffer = app["buffer"]
    last_ts = None
    try:
        while True:
            frame = buffer.latest()
            if frame is not None and frame.ts != last_ts:
                last_ts = frame.ts
                for queue in list(app["clients"]):
                    _offer(queue, frame)
            await asyncio.sleep(FRAME_INTERVAL)
    except asyncio.CancelledError:
        pass


async def _stream(request):
    app = request.app
    resp = web.StreamResponse(headers={
        "Content-Type": "multipart/x-mixed-replace; boundary=%s" % BOUNDARY,
        "Cache-Control": "no-cache",
    })
    await resp.prepare(request)

    queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    app["clients"].add(queue)
    # Seed with whatever is already buffered so a viewer sees a frame
    # immediately instead of waiting for the next broadcast tick.
    latest = app["buffer"].latest()
    if latest is not None:
        _offer(queue, latest)
    try:
        while True:
            frame = await queue.get()
            await resp.write(_part(frame))
    except (ConnectionResetError, ConnectionAbortedError, asyncio.CancelledError):
        pass
    finally:
        app["clients"].discard(queue)
    return resp


async def _start_broadcast(app):
    app["broadcast_task"] = asyncio.ensure_future(_broadcast(app))


async def _stop_broadcast(app):
    task = app.get("broadcast_task")
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def make_live_app(buffer):
    app = web.Application()
    app["buffer"] = buffer
    app["clients"] = set()
    app.router.add_get("/live", _stream)
    app.on_startup.append(_start_broadcast)
    app.on_cleanup.append(_stop_broadcast)
    return app
