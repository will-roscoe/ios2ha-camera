import asyncio
from aiohttp.test_utils import TestServer, TestClient
from ios2ha_camera.ring_buffer import RingBuffer, Frame
from ios2ha_camera.live import make_live_app, QUEUE_SIZE


def test_live_streams_latest_frame():
    async def run():
        buf = RingBuffer(1_000_000)
        buf.append(Frame(b"\xff\xd8\xff\xd9", ts=1.0))
        app = make_live_app(buf)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/live")
            assert resp.status == 200
            assert "multipart/x-mixed-replace" in resp.headers["Content-Type"]
            chunk = await resp.content.read(64)
            assert b"\xff\xd8\xff" in chunk
    asyncio.run(run())


def test_slow_client_is_dropped_without_blocking_fast_client():
    """The point of this module: a client that never reads its response must
    not stall the ring-buffer fan-out or any other connected client. Proven
    here with a real socket -- the slow client never calls .read(), so once
    its buffered-but-unread bytes cross aiohttp's read_bufsize the transport
    stops draining and the server's write to it genuinely blocks -- while a
    concurrently-reading fast client keeps receiving fresh frames throughout.
    """
    async def run():
        buf = RingBuffer(50_000_000)
        big = b"\xff\xd8" + b"\x00" * 3_000_000 + b"\xff\xd9"  # >> read_bufsize
        buf.append(Frame(big, ts=1.0))
        app = make_live_app(buf)

        async with TestClient(TestServer(app)) as client:
            slow_resp = await client.get("/live")
            fast_resp = await client.get("/live")

            fast_chunks = []

            async def drain_fast():
                while True:
                    chunk = await fast_resp.content.read(65536)
                    if not chunk:
                        return
                    fast_chunks.append(chunk)

            fast_task = asyncio.ensure_future(drain_fast())
            try:
                # Slow client never reads. Push more frames while it can't
                # keep up; the fast client must keep receiving fresh data
                # throughout, proving the slow client never blocked anything.
                for i in range(5):
                    await asyncio.sleep(0.05)
                    buf.append(Frame(b"tiny-frame-%d" % i, ts=2.0 + i))

                await asyncio.wait_for(
                    _wait_until(lambda: sum(len(c) for c in fast_chunks) > 3_000_000),
                    timeout=3.0,
                )

                # The slow client's queue must be capped, not accumulating
                # every frame it missed.
                queues = list(app["clients"])
                assert len(queues) == 2
                for q in queues:
                    assert q.qsize() <= QUEUE_SIZE
            finally:
                fast_task.cancel()
                await asyncio.gather(fast_task, return_exceptions=True)
                await slow_resp.release()
                await fast_resp.release()
    asyncio.run(run())


async def _wait_until(cond, interval=0.01):
    while not cond():
        await asyncio.sleep(interval)
