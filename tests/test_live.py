import asyncio
import time
from aiohttp.test_utils import TestServer, TestClient
from ios2ha_camera.ring_buffer import RingBuffer, Frame
from ios2ha_camera.live import CLIENTS, make_live_app, QUEUE_SIZE


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
                queues = list(app[CLIENTS])
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


def test_still_route_serves_the_stacked_still():
    """The thumbnail should be the stacked image, not a single raw frame: at
    night one frame is mostly noise."""
    buf = RingBuffer(1_000_000)
    buf.append(Frame(b"\xff\xd8\xffraw-frame\xff\xd9", ts=1.0))
    stacked = b"\xff\xd8\xffstacked-still\xff\xd9"

    async def run():
        app = make_live_app(buf, latest_still=lambda: stacked)
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/still.jpg")
            assert r.status == 200
            assert r.headers["Content-Type"] == "image/jpeg"
            assert await r.read() == stacked
    asyncio.run(run())


def test_still_route_falls_back_to_a_raw_frame_before_the_first_still():
    buf = RingBuffer(1_000_000)
    buf.append(Frame(b"\xff\xd8\xffraw-frame\xff\xd9", ts=1.0))

    async def run():
        app = make_live_app(buf, latest_still=lambda: None)
        async with TestClient(TestServer(app)) as client:
            r = await client.get("/still.jpg")
            assert r.status == 200
            assert await r.read() == b"\xff\xd8\xffraw-frame\xff\xd9"
    asyncio.run(run())


def test_still_route_says_so_when_there_is_nothing_at_all():
    async def run():
        app = make_live_app(RingBuffer(1000), latest_still=lambda: None)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/still.jpg")).status == 503
    asyncio.run(run())


class FakeUpstream:
    """Stands in for Upstream: records hold()/release() without touching the
    phone, so these tests can check a viewer's hold lifecycle in isolation."""

    def __init__(self):
        self.events = []

    def hold(self, reason, linger_s=0.0):
        outer = self

        class _H:
            def __enter__(s):
                outer.events.append("hold")
                return s

            def __exit__(s, *exc):
                outer.events.append("release")
                return False
        return _H()


def test_a_viewer_holds_the_connection_and_releases_on_disconnect():
    async def run():
        buf = RingBuffer(1 << 20)
        buf.append(Frame(b"\xff\xd8jpeg", time.time()))
        up = FakeUpstream()
        async with TestClient(TestServer(make_live_app(buf, upstream=up))) as client:
            resp = await client.get("/live")
            await resp.content.read(64)
            resp.close()
            # Bounded wait for the handler's finally to run, not a fixed
            # sleep: the disconnect is only observed asynchronously from here.
            await asyncio.wait_for(_wait_until(lambda: "release" in up.events), timeout=2.0)
            assert up.events[0] == "hold"
    asyncio.run(run())


def test_still_jpg_never_holds_the_connection():
    async def run():
        buf = RingBuffer(1 << 20)
        buf.append(Frame(b"\xff\xd8jpeg", time.time()))
        up = FakeUpstream()
        async with TestClient(TestServer(make_live_app(buf, upstream=up))) as client:
            resp = await client.get("/still.jpg")
            assert resp.status == 200
            assert up.events == []
    asyncio.run(run())


def test_the_stacked_route_serves_the_stackers_frames():
    async def run():
        buf = RingBuffer(1 << 20)

        class FakeStacker:
            def __init__(self):
                self.attached = 0

            def attach(self):
                self.attached += 1

            def detach(self):
                self.attached -= 1

            def latest(self):
                return Frame(b"\xff\xd8stacked", time.time())

        st = FakeStacker()
        app = make_live_app(buf, upstream=FakeUpstream(), stacker=st)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/live/stacked")
            chunk = await resp.content.read(256)
            assert b"stacked" in chunk
            assert st.attached == 1
            resp.close()
    asyncio.run(run())
