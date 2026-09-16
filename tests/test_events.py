"""The SSE event stream at `/api/v1/events`.

A Home Assistant custom component holds exactly one of these connections and
drives every entity from it, with no polling. So this stream is the component's
entire view of the camera, and the properties pinned here are the ones whose
failure is silent: a lost or suppressed delta leaves an entity showing a wrong
value, possibly until that key next changes, which for a setting may be never.

The rest are lifecycle properties. State is published from threads that are not
the event loop; one stalled client must not hold up anyone else; a client that
goes away, for whatever reason, must not leave anything behind; and shutting the
server down must not wait on a stream that has nothing to say.

Driven the way tests/test_live.py drives the loop: pytest-aiohttp is not
installed and is not being added.
"""

import asyncio
import json
import threading
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ios2ha_camera.api import add_api_routes, make_api, objects_version
from ios2ha_camera.events import EventHub
from ios2ha_camera.live import make_live_app
from ios2ha_camera.ring_buffer import Frame, RingBuffer
from ios2ha_camera.settings import Settings
from ios2ha_camera.supervisor import Supervisor

PATH = "/api/v1/events"


async def _next_event(resp, timeout=2.0):
    """The next SSE event as (name, decoded data), or ("keepalive", None) for the
    comment line a quiet stream sends. Raises EOFError when the stream ends."""
    fields = {}
    while True:
        # readuntil rather than readline: aiohttp's default line limit is 512 KB
        # and the drop tests deliberately publish values larger than that.
        line = await asyncio.wait_for(resp.content.readuntil(b"\n", max_size=16 << 20),
                                      timeout)
        if not line:
            raise EOFError
        line = line.decode().rstrip("\n")
        if line == "":
            if fields:
                break
            continue
        if line.startswith(":"):
            assert line == ": keepalive"
            fields = {"event": "keepalive"}
            continue
        key, _, value = line.partition(": ")
        assert key not in fields, f"repeated SSE field {key!r}"
        fields[key] = value
    if fields["event"] == "keepalive":
        return "keepalive", None
    assert set(fields) == {"event", "data"}
    return fields["event"], json.loads(fields["data"])


async def _next_non_keepalive(resp, timeout=2.0):
    while True:
        name, data = await _next_event(resp, timeout)
        if name != "keepalive":
            return name, data


async def _wait_until(cond, timeout=3.0, interval=0.01):
    async def poll():
        while not cond():
            await asyncio.sleep(interval)
    await asyncio.wait_for(poll(), timeout)


async def _publish(hub, d):
    """Publish from a real worker thread, never the loop: that is where every
    publisher in the service lives."""
    await asyncio.to_thread(hub.publish, d)


def _app(state, hub):
    return make_api(state, Settings(), events=hub)


def _run(coro):
    asyncio.run(coro)


# -- what a client is promised: a snapshot, then only changes ----------------

def test_a_new_subscriber_receives_a_full_snapshot_first():
    """Mirrors MQTT retained state: a reconnecting client must not need a separate GET,
    and must not sit with a blank entity until something happens to change."""
    async def run():
        hub = EventHub()
        store = {"exposure": 0.5, "phone_battery": 81}
        async with TestClient(TestServer(_app(lambda: dict(store), hub))) as client:
            resp = await client.get(PATH)
            assert resp.status == 200
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            assert resp.headers["Cache-Control"] == "no-store"
            name, data = await _next_event(resp)
            assert name == "snapshot"
            assert data == {"objects_version": objects_version(), "state": store}
            resp.close()
    _run(run())


def test_subsequent_events_carry_only_what_changed():
    async def run():
        hub = EventHub()
        store = {"exposure": 0.5, "phone_battery": 81}
        async with TestClient(TestServer(_app(lambda: dict(store), hub))) as client:
            resp = await client.get(PATH)
            await _next_event(resp)
            store["phone_battery"] = 80
            await _publish(hub, {"phone_battery": 80})
            assert await _next_event(resp) == ("state", {"state": {"phone_battery": 80}})
            resp.close()
    _run(run())


def test_a_slow_client_is_dropped_rather_than_stalling_the_others():
    """The failure that matters: one dashboard left open on a sleeping laptop must not
    hold up state for everything else.

    Dropped means disconnected, not thinned: a client that silently missed a delta
    would show a wrong value with nothing to tell it so, where a disconnected one
    reconnects and gets a fresh snapshot. The write timeout is set far beyond the
    test so that it is the full queue, not the timeout, that ends the slow client.
    """
    async def run():
        hub = EventHub(queue_size=4, write_timeout_s=60.0)
        async with TestClient(TestServer(_app(dict, hub))) as client:
            slow = await client.get(PATH)          # never read again after this
            fast = await client.get(PATH)
            await _next_event(fast)
            await _wait_until(lambda: hub.client_count == 2)

            got = []

            async def drain_fast():
                while True:
                    got.append(await _next_non_keepalive(fast, timeout=5.0))

            reader = asyncio.ensure_future(drain_fast())
            try:
                # Each event is far bigger than the socket buffers, so the slow
                # client's write genuinely blocks and its queue fills behind it.
                # Publishing waits for the fast client each time, so that when
                # one of them is dropped it is unambiguously the one that
                # stopped reading.
                blob = "x" * 1_000_000
                published = 0
                for i in range(60):
                    await _publish(hub, {"big": f"{i}:{blob}"})
                    published = i + 1
                    await _wait_until(lambda n=published: len(got) == n, timeout=10.0)
                    if hub.client_count == 1:
                        break
                assert hub.client_count == 1
                # Every delta, in order: the fast client lost nothing.
                assert [d["state"]["big"].split(":")[0] for _, d in got] == \
                    [str(i) for i in range(published)]
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
            # And the slow client's connection really was ended, not just forgotten.
            with pytest.raises(Exception):  # noqa: B017 -- EOF or a reset, either proves it
                while True:
                    await asyncio.wait_for(slow.content.read(1 << 20), 5.0)
                    if slow.content.at_eof():
                        raise EOFError
            fast.close()
            slow.close()
            await _wait_until(lambda: hub.client_count == 0)
    _run(run())


def test_objects_changed_is_emitted_when_the_registry_version_changes():
    async def run():
        hub = EventHub()
        async with TestClient(TestServer(_app(dict, hub))) as client:
            resp = await client.get(PATH)
            await _next_event(resp)
            await asyncio.to_thread(hub.set_objects_version, "sha256:new")
            assert await _next_event(resp) == (
                "objects_changed", {"objects_version": "sha256:new"})
            # The same value again is not a change; the marker proves nothing
            # was emitted in between.
            await asyncio.to_thread(hub.set_objects_version, "sha256:new")
            await _publish(hub, {"marker": 1})
            assert await _next_event(resp) == ("state", {"state": {"marker": 1}})
            resp.close()
            # A reconnecting client learns the new version from its snapshot.
            resp = await client.get(PATH)
            _, data = await _next_event(resp)
            assert data["objects_version"] == "sha256:new"
            resp.close()
    _run(run())


# -- what counts as a change -------------------------------------------------

def test_republishing_unchanged_values_emits_nothing():
    """The health loop republishes its whole dict every tick whether or not
    anything moved; MQTT's retained topics absorb that, a client stream should
    not have to."""
    async def run():
        hub = EventHub()
        async with TestClient(TestServer(_app(dict, hub))) as client:
            resp = await client.get(PATH)
            await _next_event(resp)
            full = {"cpu_temp": 41.3, "uptime": 10, "mode": "day", "levels": [1, 2]}
            await _publish(hub, full)
            await _publish(hub, dict(full))
            await _publish(hub, {**full, "uptime": 11})
            assert await _next_event(resp) == ("state", {"state": full})
            assert await _next_event(resp) == ("state", {"state": {"uptime": 11}})
            # Nothing at all for an empty or all-unchanged publish.
            await _publish(hub, {})
            await _publish(hub, {"uptime": 11})
            await _publish(hub, {"marker": True})
            assert await _next_event(resp) == ("state", {"state": {"marker": True}})
            resp.close()
    _run(run())


def test_a_value_is_compared_as_it_is_served_not_as_python_sees_it():
    """0 == False and 1 == 1.0 in Python, but a client that was sent `false` and is
    now owed `0` has been told something different. A tuple and a list, though,
    are the same JSON and are not a change."""
    async def run():
        hub = EventHub()
        async with TestClient(TestServer(_app(dict, hub))) as client:
            resp = await client.get(PATH)
            await _next_event(resp)
            await _publish(hub, {"a": False, "b": (1, 2), "c": 0.1})
            await _publish(hub, {"a": 0, "b": [1, 2], "c": 0.1})
            assert await _next_event(resp) == (
                "state", {"state": {"a": False, "b": [1, 2], "c": 0.1}})
            assert await _next_event(resp) == ("state", {"state": {"a": 0}})
            resp.close()
    _run(run())


def test_a_value_that_changes_and_changes_back_is_not_suppressed():
    """The snapshot is read fresh from `state` on connect, so it can be newer than
    anything published. If the hub compared the next publish with what it last
    *published* rather than what clients were last *told*, a value that moved and
    then moved back would be swallowed and the new client left on the wrong one."""
    async def run():
        hub = EventHub()
        store = {"mode": "day"}
        async with TestClient(TestServer(_app(lambda: dict(store), hub))) as client:
            await _publish(hub, {"mode": "day"})          # published once, nobody listening
            store["mode"] = "night"                       # changed without a publish
            resp = await client.get(PATH)
            assert (await _next_event(resp))[1]["state"] == {"mode": "night"}
            store["mode"] = "day"
            await _publish(hub, {"mode": "day"})
            assert await _next_event(resp) == ("state", {"state": {"mode": "day"}})
            resp.close()
    _run(run())


def test_a_fresh_snapshot_value_reaches_clients_already_connected():
    """Every client must hold the same view. A value a new client's snapshot
    picked up is a change the others have not been told about."""
    async def run():
        hub = EventHub()
        store = {"uptime": 1}
        async with TestClient(TestServer(_app(lambda: dict(store), hub))) as client:
            first = await client.get(PATH)
            await _next_event(first)
            store["uptime"] = 2                           # moved without a publish
            second = await client.get(PATH)
            assert (await _next_event(second))[1]["state"] == {"uptime": 2}
            assert await _next_event(first) == ("state", {"state": {"uptime": 2}})
            # And a publish of that same value is then no change to anyone.
            await _publish(hub, {"uptime": 2, "marker": 1})
            assert await _next_event(first) == ("state", {"state": {"marker": 1}})
            assert await _next_event(second) == ("state", {"state": {"marker": 1}})
            first.close()
            second.close()
    _run(run())


# -- threads and ordering ----------------------------------------------------

def test_publishing_from_a_plain_thread_is_delivered_and_never_blocks():
    """Publishers are the health, ingest and device threads and paho's network
    thread. None of them may stall on a client, even one that has stopped reading."""
    async def run():
        hub = EventHub(write_timeout_s=60.0)
        async with TestClient(TestServer(_app(dict, hub))) as client:
            stalled = await client.get(PATH)           # never read
            resp = await client.get(PATH)
            await _next_event(resp)
            await _wait_until(lambda: hub.client_count == 2)
            timings = []

            def publisher():
                blob = "y" * 100_000
                for i in range(200):
                    t0 = time.monotonic()
                    hub.publish({"n": i, "blob": f"{i}{blob}"})
                    timings.append(time.monotonic() - t0)

            t = threading.Thread(target=publisher)
            t.start()
            seen = []
            while len(seen) < 200:
                _, data = await _next_non_keepalive(resp, timeout=5.0)
                seen.append(data["state"]["n"])
            await asyncio.to_thread(t.join, 5.0)
            assert seen == list(range(200))
            assert max(timings) < 0.1
            stalled.close()
            resp.close()
    _run(run())


def test_nothing_a_publisher_passes_can_raise_back_at_it():
    """`publish` is called from paho's network thread and from the health and
    ingest threads. An exception there does not just lose an event, it takes
    down the loop that was publishing -- so a bad argument is logged and
    dropped, and the keys around it still go out."""
    async def run():
        hub = EventHub()
        async with TestClient(TestServer(_app(dict, hub))) as client:
            resp = await client.get(PATH)
            await _next_event(resp)
            for bad in ("not a mapping", 17, object(), [("a", 1)]):
                await asyncio.to_thread(hub.publish, bad)
            # One unencodable value costs that key, not the publish.
            await _publish(hub, {"ok": 1, "bad": object(), "temp": float("nan"),
                                 "over": float("inf")})
            assert await _next_event(resp) == ("state", {"state": {"ok": 1}})
            resp.close()
    _run(run())


def test_publishing_with_no_loop_is_silently_dropped():
    """Before the server starts there is nobody to tell, and the first
    subscriber's snapshot covers anything published in the meantime."""
    hub = EventHub()
    hub.publish({"a": 1})
    hub.set_objects_version("sha256:x")

    async def run():
        async with TestClient(TestServer(_app(lambda: {"a": 1}, hub))) as client:
            resp = await client.get(PATH)
            assert await _next_event(resp) == (
                "snapshot", {"objects_version": objects_version(), "state": {"a": 1}})
            resp.close()
    _run(run())
    # And after the server has gone, publishing is still harmless.
    hub.publish({"a": 2})


def test_a_delta_published_while_the_snapshot_is_read_is_not_lost():
    """The race at the heart of the module: a publisher changes the state and
    publishes just after the snapshot was read. The snapshot does not have the
    new value, so the delta must follow it."""
    async def run():
        hub = EventHub()
        store = {"late": 1}

        def state():
            snap = dict(store)
            if not reads:
                # Exactly the interleaving that matters: another thread updates
                # and publishes between the snapshot read and the stream starting.
                t = threading.Thread(target=lambda: (store.update(late=2),
                                                     hub.publish({"late": 2})))
                t.start()
                t.join()
            reads.append(snap)
            return snap

        reads = []
        async with TestClient(TestServer(_app(state, hub))) as client:
            resp = await client.get(PATH)
            assert await _next_event(resp) == (
                "snapshot", {"objects_version": objects_version(), "state": {"late": 1}})
            assert await _next_event(resp) == ("state", {"state": {"late": 2}})
            resp.close()
    _run(run())


def test_no_delta_is_lost_to_a_client_connecting_mid_stream():
    """Clients connect while a thread publishes steadily. Each key is written once,
    so a missing key in a client's reconstructed state is a lost delta."""
    async def run():
        hub = EventHub()
        store = {}
        lock = threading.Lock()

        def state():
            with lock:
                return dict(store)

        def publisher():
            for i in range(600):
                with lock:
                    store[f"k{i}"] = i
                hub.publish({f"k{i}": i})
                if i % 50 == 0:
                    time.sleep(0.001)

        async def follow(client, view):
            resp = await client.get(PATH)
            try:
                while view.get("k599") != 599:
                    name, data = await _next_non_keepalive(resp, timeout=5.0)
                    view.update(data["state"])
            finally:
                resp.close()

        async with TestClient(TestServer(_app(state, hub))) as client:
            t = threading.Thread(target=publisher)
            views = [{} for _ in range(8)]
            t.start()
            followers = []
            for view in views:
                followers.append(asyncio.ensure_future(follow(client, view)))
                await asyncio.sleep(0.002)
            await asyncio.wait_for(asyncio.gather(*followers), 15.0)
            await asyncio.to_thread(t.join, 5.0)
            for view in views:
                assert view == {f"k{i}": i for i in range(600)}
    _run(run())


# -- keepalive, the phone, leaks and shutdown ---------------------------------

async def _as_deployed(server):
    """Start `server` the way the service runs, not the way aiohttp's test
    harness does.

    `TestServer` starts its runner with `handler_cancellation=True`, so a
    dropped connection cancels the request task for it. `web.run_app` leaves
    that off, which is the default and what the service uses: there, nothing
    wakes a handler when its connection goes -- it finds out at its next write.
    A lifecycle test run under the harness default would pass on cancellation
    the deployment never performs.
    """
    await server.start_server()
    server.runner.server.handler_cancellation = False
    return server


def test_an_aborted_client_ends_at_once_not_at_its_next_keepalive():
    """A stream parked waiting for events is aborted when its queue fills and at
    shutdown. Aborting the socket alone is not felt by a task waiting on the
    queue -- with nothing cancelling it, it would find out at its next write, a
    keepalive later, which is longer than the service's whole stop grace period.

    Deliberately runs with the shipped keepalive, not a tiny test one: a test
    that injects 50 ms cannot tell a prompt end from a late one.
    """
    async def run():
        hub = EventHub()
        assert hub._keepalive_s >= 15.0, "this test is only meaningful with the real interval"
        server = await _as_deployed(TestServer(_app(dict, hub)))
        async with TestClient(server) as client:
            resp = await client.get(PATH)
            await _next_event(resp)
            await _wait_until(lambda: hub.client_count == 1)
            t0 = time.monotonic()
            for c in list(hub._clients):
                c.abort()
            await _wait_until(lambda: hub.client_count == 0, timeout=2.0)
            assert time.monotonic() - t0 < 2.0
            resp.close()
    _run(run())


def test_the_stream_asks_nginx_not_to_buffer_it():
    """nginx buffers a proxied response by default, which would hold back the
    keepalives that exist to stop a proxy closing the connection."""
    async def run():
        hub = EventHub()
        async with TestClient(TestServer(_app(dict, hub))) as client:
            resp = await client.get(PATH)
            assert resp.headers["X-Accel-Buffering"] == "no"
            resp.close()
    _run(run())


def test_a_quiet_stream_sends_keepalives():
    """A Cloudflare tunnel closes an HTTP connection that says nothing for about
    100 s, and the state can be quiet for much longer than that."""
    async def run():
        hub = EventHub(keepalive_s=0.05)
        async with TestClient(TestServer(_app(dict, hub))) as client:
            resp = await client.get(PATH)
            await _next_event(resp)
            assert await _next_event(resp, timeout=1.0) == ("keepalive", None)
            assert await _next_event(resp, timeout=1.0) == ("keepalive", None)
            resp.close()
    _run(run())


class _RecordingUpstream:
    def __init__(self):
        self.holds = []

    def hold(self, reason, linger_s=0.0):
        self.holds.append(reason)
        raise AssertionError("an events subscriber must never hold the phone")


def test_an_events_subscriber_never_holds_the_phone():
    """State is cheap. Keeping the phone's radio up because a dashboard is
    watching a sensor would drain its battery for nothing -- so, unlike /live,
    this route takes no upstream hold even on the application that has one."""
    async def run():
        buf = RingBuffer(1 << 20)
        buf.append(Frame(b"\xff\xd8jpeg", time.time()))
        up = _RecordingUpstream()
        hub = EventHub()
        app = make_live_app(buf, upstream=up)
        add_api_routes(app, lambda: {"a": 1}, Settings(), events=hub)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(PATH)
            assert (await _next_event(resp))[0] == "snapshot"
            await _publish(hub, {"a": 2})
            await _next_event(resp)
            assert up.holds == []
            resp.close()
    _run(run())


def test_head_is_not_served_as_an_endless_stream():
    """A HEAD has no body to stream, and a handler that kept one open would sit
    there until shutdown."""
    async def run():
        hub = EventHub()
        async with TestClient(TestServer(_app(dict, hub))) as client:
            resp = await client.head(PATH)
            assert resp.status == 405
            assert hub.client_count == 0
    _run(run())


def _other_tasks(baseline):
    return {t for t in asyncio.all_tasks() - baseline if not t.done()} - {asyncio.current_task()}


@pytest.mark.parametrize("how", ["client_closes", "write_times_out", "queue_overflows"])
def test_nothing_is_left_behind_after_a_disconnect(how):
    async def run():
        hub = EventHub(keepalive_s=0.05,
                       write_timeout_s=0.2 if how == "write_times_out" else 60.0,
                       queue_size=4 if how == "queue_overflows" else 1000)
        server = await _as_deployed(TestServer(_app(dict, hub)))
        async with TestClient(server) as client:
            baseline = set(asyncio.all_tasks())
            resp = await client.get(PATH)
            await _next_event(resp)
            await _wait_until(lambda: hub.client_count == 1)
            if how == "client_closes":
                resp.close()
            else:
                blob = "z" * 1_000_000
                for i in range(40):
                    await _publish(hub, {"big": f"{i}{blob}"})
            await _wait_until(lambda: hub.client_count == 0, timeout=5.0)
            resp.close()
            await _wait_until(lambda: not _other_tasks(baseline), timeout=5.0)
    _run(run())


def test_shutdown_ends_open_streams_without_waiting_out_the_keepalive():
    """aiohttp waits for running handlers before it stops; an idle stream parked
    on a long keepalive would make the service overrun its stop grace period."""
    async def run():
        hub = EventHub(keepalive_s=60.0)
        server = TestServer(_app(dict, hub))
        client = TestClient(server)
        await _as_deployed(server)
        await client.start_server()   # the server is already up; this starts the session
        try:
            resp = await client.get(PATH)
            await _next_event(resp)
            t0 = time.monotonic()
            await asyncio.wait_for(server.close(), 10.0)
            assert time.monotonic() - t0 < 1.0
            with pytest.raises(EOFError):
                await _next_event(resp, timeout=1.0)
            assert hub.client_count == 0
            # A publish after the loop has let go of the hub is harmless.
            hub.publish({"a": 1})
            resp.close()
        finally:
            await client.close()
    _run(run())


def test_the_info_document_now_names_a_served_route():
    async def run():
        hub = EventHub()
        async with TestClient(TestServer(_app(dict, hub))) as client:
            info = await (await client.get("/api/v1/info")).json()
            resp = await client.get(info["routes"]["events"])
            assert resp.status == 200
            resp.close()
    _run(run())


def test_make_api_supplies_a_hub_of_its_own():
    async def run():
        async with TestClient(TestServer(make_api(lambda: {"a": 1}, Settings()))) as client:
            resp = await client.get(PATH)
            assert (await _next_event(resp))[1]["state"] == {"a": 1}
            resp.close()
    _run(run())


# -- the supervisor as publisher: the stream and /state must agree -----------

class _MqttThatStallsOnTrue:
    """A broker client whose publish of `True` stalls until the next publish
    has been stored and heard, or until a short wait runs out.

    That is the slow network path a real publisher can hit between storing a
    value and telling the listeners about it, forced here so the interleaving
    happens on every run rather than once in a thousand. With the publish
    sequence serialised, the second publisher cannot get in during the stall,
    and the wait simply runs out.
    """

    def __init__(self):
        self.second_done = threading.Event()

    def publish_state(self, d):
        if d.get("autofocus") is True:
            self.second_done.wait(0.5)

    def __getattr__(self, name):
        return lambda *a, **k: None


def test_two_threads_publishing_one_key_leave_the_stream_agreeing_with_state():
    """Two threads publish the same key; the value clients were told last must be
    the value /api/v1/state serves. Otherwise every event client holds one value
    and a poller another, until that key is next published -- which, for a
    setting with MQTT off, may be never."""
    async def run():
        mq = _MqttThatStallsOnTrue()
        sup = Supervisor(Settings(), RingBuffer(1_000_000), mq, None)
        hub = EventHub()
        sup.add_state_listener(hub.publish)
        async with TestClient(TestServer(_app(sup.current_state, hub))) as client:
            resp = await client.get(PATH)
            name, data = await _next_event(resp)
            assert name == "snapshot"
            view = dict(data["state"])

            def first():
                sup.publish_state({"autofocus": True})

            def second():
                # Starts once the first has stored its value and is stalled.
                while sup.current_state().get("autofocus") is not True:
                    time.sleep(0.001)
                sup.publish_state({"autofocus": False})
                mq.second_done.set()

            await asyncio.gather(asyncio.to_thread(first), asyncio.to_thread(second))
            try:
                while True:
                    name, data = await _next_non_keepalive(resp, timeout=0.5)
                    if name == "state":
                        view.update(data["state"])
            except TimeoutError:
                pass
            assert view["autofocus"] == sup.current_state()["autofocus"]
            resp.close()
        sup.close()
    _run(run())
