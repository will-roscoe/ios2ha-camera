import io
import threading
import time

import numpy as np
import pytest
from PIL import Image

from ios2ha_camera import stack
from ios2ha_camera.mqtt_io import discovery_payloads
from ios2ha_camera.phone import PhoneUnavailable
from ios2ha_camera.ring_buffer import Frame, RingBuffer
from ios2ha_camera.settings import CONTROLS, Settings
from ios2ha_camera.supervisor import Supervisor, estimate_fps


class FakeMqtt:
    def __init__(self):
        self.stills = []
        self.states = []
        self.events = []
        self.screenshots = []

    def publish_still(self, j):
        self.stills.append(j)

    def publish_state(self, d):
        self.states.append(d)

    def publish_event(self, d):
        self.events.append(d)

    def publish_screenshot(self, png):
        self.screenshots.append(png)

    def set_available(self, ok):
        pass

    @property
    def published(self):
        """Every state key published so far, flattened."""
        out = {}
        for d in self.states:
            out.update(d)
        return out


class FakeArchive:
    def __init__(self, free=50 * 2**30):
        self.paths = []
        self.free = free

    def write_still(self, j, ts):
        self.paths.append("p")
        return "p"

    def free_bytes(self):
        return self.free


class FakeOnvif:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    def set_autofocus(self, mode):
        self.calls.append(mode)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakePhone:
    def __init__(self, battery=None, error=None, running=False, running_error=None):
        self._battery = battery or {"level": 88, "temperature": 27.5, "charging": True,
                                    "current_ma": 322, "charger_watts": 3, "health_pct": 80.2}
        self._error = error
        self._running = running
        self._running_error = running_error
        self.launched = 0

    def battery(self):
        if self._error:
            raise self._error
        return self._battery

    def screenshot(self):
        return b"\x89PNG\r\n\x1a\n fake"

    def is_running(self):
        if self._running_error:
            raise self._running_error
        return self._running

    def launch(self):
        self.launched += 1
        return True


class Inline:
    """Runs device actions on the calling thread so tests stay deterministic."""

    def submit(self, fn, *a):
        fn(*a)

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


def _fill(buf, n, val=30, now=None, step=1 / 12):
    now = time.time() if now is None else now
    for i in range(n):
        rng = np.random.default_rng(i)
        a = np.clip(val + rng.normal(0, 12, (32, 32, 3)), 0, 255).astype("uint8")
        b = io.BytesIO()
        Image.fromarray(a).save(b, "JPEG", quality=92)
        buf.append(Frame(b.getvalue(), ts=now - (n - 1 - i) * step))


def _sup(settings=None, buf=None, mqtt=None, archive=None, **kw):
    s = settings or Settings()
    # Inline unless a test wants to watch what was queued rather than run it.
    executor = kw.pop("device_executor", None) or Inline()
    return Supervisor(s, buf or RingBuffer(5_000_000), mqtt or FakeMqtt(),
                      archive or FakeArchive(), device_executor=executor, **kw)


def test_produce_still_publishes_and_archives():
    s = Settings()
    s.stacking_mode = "fixed"
    s.fixed_depth = 10
    buf = RingBuffer(5_000_000)
    _fill(buf, 20)
    mq, ar = FakeMqtt(), FakeArchive()
    sup = _sup(s, buf, mq, ar)
    ev = sup.produce_still(now_fps=12.0)
    assert mq.stills and ar.paths and mq.events
    assert ev["depth_used"] >= 1
    assert any("depth_used" in st for st in mq.states)


def test_probe_mean_matches_np_mean_without_materialising_the_stacked_probe(monkeypatch):
    """np.mean(probe, axis=0) (supervisor.py:285) implicitly stacks every decoded
    probe frame into one (N,H,W,3) array before reducing -- ~373MB for
    probe_n=15 at 1920x1080 float32, thrown away immediately -- while the phone
    connection is held open for the whole capture (supervisor.py:265's `with
    hold:`). sum(probe)/len(probe) measured 73-167ms against np.mean's
    200ms-1.1s and is bit-identical for stack.decode's float32 output
    (sweep-performance.md finding 1): assert both that the expensive call is
    gone and that the replacement's numbers pin exactly to what np.mean would
    have given, for the same frames."""
    from ios2ha_camera import measure

    real_mean = np.mean
    list_calls = []

    def spy_mean(a, *args, **kwargs):
        # The only np.mean(<list>, axis=0) call anywhere in this codebase is the
        # probe-averaging line finding 1 targets -- every other np.mean call
        # here reduces a single ndarray, so a list argument uniquely identifies it.
        if isinstance(a, list):
            list_calls.append(a)
        return real_mean(a, *args, **kwargs)
    monkeypatch.setattr(np, "mean", spy_mean)

    captured = {}
    real_black_level = measure.black_level

    def spy_black_level(mean, *a, **kw):
        captured["mean"] = mean
        return real_black_level(mean, *a, **kw)
    monkeypatch.setattr(measure, "black_level", spy_black_level)

    s = Settings()
    s.stacking_mode = "fixed"
    s.fixed_depth = 10
    buf = RingBuffer(5_000_000)
    now = time.time()
    _fill(buf, 20, now=now)
    sup = _sup(s, buf)
    sup.produce_still(now_fps=12.0, now=now)

    assert list_calls == [], (
        "np.mean(probe, axis=0) still materialises the whole probe stack")
    assert "mean" in captured

    probe_frames = sup._fresh(buf.recent(sup.probe_n), now, sup.fresh_s)
    reference = real_mean([stack.decode(f) for f in probe_frames], axis=0)
    assert captured["mean"].dtype == np.float32
    assert np.array_equal(captured["mean"], reference)


def test_handle_control_updates_setting():
    s = Settings()
    sup = _sup(s)
    sup.handle_control("render_intent", "inspection")
    assert s.render_intent == "inspection"


def test_every_discovery_sensor_gets_a_value():
    """Guards the seam between discovery and the supervisor: an entity Home
    Assistant is told about but never receives a value for shows as unknown
    forever."""
    s = Settings()
    s.stacking_mode = "fixed"
    s.fixed_depth = 8
    buf = RingBuffer(5_000_000)
    _fill(buf, 20)
    mq = FakeMqtt()
    sup = _sup(s, buf, mq, phone=FakePhone(), onvif=FakeOnvif())
    sup.on_stream_state("streaming")
    sup.produce_still(now_fps=12.0)
    sup.publish_state(sup.health_state())
    sup.publish_state(sup.phone_state())
    sup.publish_controls()

    # holders, live_fps, duty_1h and tick_age are only populated with an
    # upstream and a live stacker wired in, as __main__ does; a second
    # supervisor sharing the same mqtt fake stands in for that wiring so this
    # test still covers every declared sensor.
    up_sup = _sup(s, buf, mq, upstream=FakeUpstream(state="streaming"))
    up_sup.live_fps = lambda: 12.3
    up_sup.publish_state(up_sup.health_state())

    declared = set()
    for topic, _cfg in discovery_payloads(s):
        parts = topic.split("/")
        if parts[1] in ("sensor", "binary_sensor", "number", "select", "switch"):
            declared.add(parts[3])
    # The three black-level numbers are overrides with no value until one is
    # set: 0 would read as "override to zero" and the auto-estimated level
    # would make an inactive override look active. Home Assistant shows an
    # unknown number as blank and still accepts a write, which is the meaning
    # wanted. test_black_level_channel_control_applies_and_publishes covers
    # the case where one is set.
    declared -= {"black_level_0", "black_level_1", "black_level_2"}
    missing = declared - set(mq.published)
    assert missing == set(), f"declared but never published: {sorted(missing)}"


def test_stale_frames_do_not_make_a_still():
    """A still built from frames captured before the stream died would be
    archived as if it were current."""
    buf = RingBuffer(5_000_000)
    _fill(buf, 20, now=time.time() - 600)
    mq, ar = FakeMqtt(), FakeArchive()
    sup = _sup(buf=buf, mqtt=mq, archive=ar)
    ev = sup.produce_still(now_fps=12.0)
    assert ev["error"] == "no fresh frames"
    assert not mq.stills and not ar.paths


def test_frames_older_than_the_window_are_excluded_and_flagged():
    s = Settings()
    s.stacking_mode = "fixed"
    s.fixed_depth = 30
    s.window_max_s = 1.0
    buf = RingBuffer(5_000_000)
    now = time.time()
    _fill(buf, 10, now=now - 20, step=0.05)   # old but inside the buffer
    _fill(buf, 6, now=now, step=0.05)         # inside the window
    mq = FakeMqtt()
    sup = _sup(s, buf, mq)
    ev = sup.produce_still(now_fps=12.0, now=now)
    assert ev["depth_used"] == 6, "frames from before the window were stacked"
    # The requested depth is itself clamped to fps * window_max (12), below the
    # fixed_depth of 30; what matters is that fewer frames were available than
    # were asked for, and that the event says so.
    assert ev["depth_requested"] == 12
    assert ev["short"] is True


def test_archive_is_skipped_when_the_disk_is_low_but_the_still_still_publishes():
    s = Settings()
    s.stacking_mode = "fixed"
    s.fixed_depth = 5
    buf = RingBuffer(5_000_000)
    _fill(buf, 10)
    mq, ar = FakeMqtt(), FakeArchive(free=1 * 2**30)
    sup = _sup(s, buf, mq, ar, min_free_bytes=2 * 2**30)
    ev = sup.produce_still(now_fps=12.0)
    assert ar.paths == []
    assert ev["archived"] is False and ev["archive_skipped"] == "low_disk"
    assert mq.stills, "the live still must keep working when only the disk is full"


def test_produce_still_passes_the_measured_weight_to_render(monkeypatch):
    """The day/night blend has to be derived from the subject level this very
    capture measured, not a stale or default value -- a recording stub in
    place of render.render is enough to prove the number actually arrives."""
    from ios2ha_camera import render

    s = Settings()
    s.stacking_mode = "fixed"       # depth law's target_level plays no part in fixed mode
    s.fixed_depth = 6
    # Chosen so the measured subject (~5.05 for this buffer, fixed by the
    # deterministic per-frame seeds in _fill) lands mid-blend rather than at
    # the 1.0 the stub already defaults to -- otherwise a supervisor that
    # forgot to pass night_weight at all would pass this test by accident.
    s.target_level = 10.0
    buf = RingBuffer(5_000_000)
    _fill(buf, 10)
    mq = FakeMqtt()
    sup = _sup(s, buf, mq)

    calls = []

    def stub_render(stacked, settings, applied=None, night_weight=1.0):
        calls.append(night_weight)
        return np.zeros((2, 2, 3), dtype=np.uint8)

    monkeypatch.setattr(render, "render", stub_render)
    ev = sup.produce_still(now_fps=12.0)
    assert calls, "render.render was never called"
    expected = render.night_weight(ev["subject_level"], s.target_level)
    assert 0.0 < expected < 1.0, "test setup must land mid-blend, not at an edge"
    assert calls[0] == pytest.approx(expected, abs=0.02)


def test_published_calibration_is_what_render_applied():
    """If the supervisor recomputed gains separately from render, the sensors
    could report numbers the image was never built with."""
    from ios2ha_camera import render

    s = Settings()
    s.stacking_mode = "fixed"
    s.fixed_depth = 6
    s.white_balance = "grey-world"
    buf = RingBuffer(5_000_000)
    _fill(buf, 10)
    mq = FakeMqtt()
    sup = _sup(s, buf, mq)
    sup.produce_still(now_fps=12.0)
    stacked = sup.last_stack
    black, gains = render.calibration(stacked, s)
    pub = mq.published
    assert pytest.approx(float(gains[0]), abs=1e-3) == pub["wb_gain_r"]
    assert pytest.approx(float(black[2]), abs=1e-3) == pub["black_level_applied_b"]


def test_capture_now_does_not_stack_on_the_calling_thread():
    """produce_still takes tens of seconds on a night stack. Running it on the
    MQTT network thread would blow past the broker keepalive."""
    buf = RingBuffer(5_000_000)
    _fill(buf, 20)
    mq = FakeMqtt()
    sup = _sup(buf=buf, mqtt=mq)
    sup.handle_control("capture_now", "PRESS")
    assert sup.capture_requested.is_set()
    assert mq.stills == []


def test_a_failing_device_control_never_raises_into_the_mqtt_thread():
    sup = _sup(onvif=FakeOnvif(result=TimeoutError("no route to phone")))
    sup.handle_control("autofocus", True)          # must not raise
    assert sup.mqtt.events
    assert "TimeoutError" in sup.mqtt.events[-1]["error"]


def test_autofocus_republishes_the_real_state_when_the_camera_rejects_it():
    s = Settings()
    s.autofocus = True
    sup = _sup(s, onvif=FakeOnvif(result=False))
    sup.handle_control("autofocus", False)
    assert s.autofocus is True, "a rejected write must not change the setting"
    assert sup.mqtt.published["autofocus"] is True, "Home Assistant must be corrected back"


def test_autofocus_success_applies_and_publishes():
    s = Settings()
    s.autofocus = True
    onvif = FakeOnvif(result=True)
    sup = _sup(s, onvif=onvif)
    sup.handle_control("autofocus", False)
    assert onvif.calls == ["MANUAL"]
    assert s.autofocus is False and sup.mqtt.published["autofocus"] is False


def test_autofocus_coerces_a_conventional_string_spelling_like_the_other_controls():
    """handle_control used to coerce the raw payload with a bare bool(value)
    before ever reaching Settings.apply_control's boolean validation, so a
    non-JSON "off" (bool("off") is True) silently turned autofocus ON --
    exactly backwards. It must go through the same _coerce_bool the other
    four boolean controls use."""
    s = Settings()
    s.autofocus = True
    onvif = FakeOnvif(result=True)
    sup = _sup(s, onvif=onvif)
    sup.handle_control("autofocus", "off")
    assert onvif.calls == ["MANUAL"]
    assert s.autofocus is False and sup.mqtt.published["autofocus"] is False


def test_autofocus_rejects_unparseable_junk_before_touching_the_device_executor():
    """The rejection must happen before anything is submitted to the device
    executor: a bad value must not cost an ONVIF round trip or occupy the
    single device worker, and the stored setting must be untouched -- the
    same contract the other four boolean controls get via _apply_setting's
    rejected-control path."""
    s = Settings()
    s.autofocus = False
    onvif = FakeOnvif(result=True)
    sup = _sup(s, onvif=onvif)
    sup.handle_control("autofocus", "banana")
    assert onvif.calls == [], "a rejected value must never reach the device executor"
    assert s.autofocus is False, "a rejected write must not change the setting"
    assert sup.mqtt.published["autofocus"] is False, "Home Assistant must be corrected back"


def test_screenshot_publishes_a_png():
    sup = _sup(phone=FakePhone())
    sup.handle_control("screenshot", "PRESS")
    assert sup.mqtt.screenshots and sup.mqtt.screenshots[0].startswith(b"\x89PNG")


def test_screenshot_is_also_kept_for_the_http_route():
    """The MQTT topic is retained, so the HTTP route has to hold the same last
    image or the two would disagree about what the last screenshot was."""
    sup = _sup(phone=FakePhone())
    assert sup.last_screenshot_png is None, "nothing before the first press"
    sup.handle_control("screenshot", "PRESS")
    assert sup.last_screenshot_png == sup.mqtt.screenshots[-1]


def test_relaunch_app_calls_the_phone_and_reports_whether_it_was_running():
    """This button is for a dead app; relaunching a live one loses the stream
    it was already serving, so the event has to say which happened."""
    phone = FakePhone(running=True)
    sup = _sup(phone=phone)
    sup.handle_control("relaunch_app", "PRESS")
    assert phone.launched == 1
    assert sup.mqtt.events[-1]["was_running"] is True


def test_relaunch_still_happens_when_the_running_check_fails():
    """is_running goes over the DVT channel, which needs the developer image
    mounted -- least likely to work on exactly the reboot that killed the app.
    A diagnostic must never defeat the recovery it is describing."""
    phone = FakePhone(running_error=PhoneUnavailable("developer image not mounted"))
    sup = _sup(phone=phone)
    sup.handle_control("relaunch_app", "PRESS")
    assert phone.launched == 1, "the running check blocked the relaunch"
    assert sup.mqtt.events[-1]["was_running"] is None
    assert sup.mqtt.events[-1]["result"] == "launched"


def test_a_value_outside_a_selects_options_is_rejected_and_corrected():
    """Home Assistant's select constrains a person, but a script or
    mosquitto_pub can publish anything, and silently falling back to a default
    mid-render is the kind of quiet wrongness this project exists to avoid."""
    s = Settings()
    sup = _sup(s)
    sup.handle_control("white_balance", "sepia")      # must not raise
    assert s.white_balance == "as-shot"
    assert sup.mqtt.published["white_balance"] == "as-shot"


def test_a_valid_select_value_still_applies():
    s = Settings()
    sup = _sup(s)
    sup.handle_control("white_balance", "grey-world")
    assert s.white_balance == "grey-world"
    assert sup.mqtt.published["white_balance"] == "grey-world"


def test_unknown_control_is_ignored_quietly():
    sup = _sup()
    sup.handle_control("not_a_control", 1)
    assert sup.mqtt.states == []


def test_black_level_channel_control_applies_and_publishes():
    s = Settings()
    sup = _sup(s)
    sup.handle_control("black_level_1", 0.46)
    assert s.black_level_override == (0.0, 0.46, 0.0)
    assert sup.mqtt.published["black_level_1"] == 0.46


def test_a_non_number_for_a_black_level_channel_republishes_the_real_value(caplog):
    """Home Assistant shows a number as changed as soon as it sends it; a rejected
    write has to be answered with the value actually in effect."""
    s = Settings()
    s.black_level_override = (0.1, 0.2, 0.3)
    sup = _sup(s)
    with caplog.at_level("WARNING"):
        sup.handle_control("black_level_1", None)
    assert s.black_level_override == (0.1, 0.2, 0.3)
    assert sup.mqtt.states[-1] == {"black_level_1": 0.2}
    assert any("rejected control black_level_1" in r.getMessage() for r in caplog.records)


def test_health_state_is_offline_when_frames_are_stale():
    buf = RingBuffer(5_000_000)
    newest = time.time() - 300
    _fill(buf, 3, now=newest)
    sup = _sup(buf=buf)
    sup.on_stream_state("streaming")
    h = sup.health_state()
    assert h["camera_online"] is False
    # A timestamp, not an age: an age is only true at the moment it is
    # published, and Home Assistant can render the relative time itself.
    from datetime import datetime
    assert datetime.fromisoformat(h["last_frame"]).timestamp() == pytest.approx(newest, abs=1)


def test_health_state_reports_no_frame_time_before_any_frame():
    sup = _sup()
    assert sup.health_state()["last_frame"] is None


def test_health_state_is_online_while_frames_are_arriving():
    buf = RingBuffer(5_000_000)
    _fill(buf, 5)
    sup = _sup(buf=buf)
    sup.on_stream_state("streaming")
    assert sup.health_state()["camera_online"] is True


def test_stream_state_is_published_the_moment_it_changes():
    """Health is published on its own short tick, but a state change must not
    wait for it: the whole point is noticing failure in minutes."""
    sup = _sup()
    sup.on_stream_state("streaming_off")
    assert sup.mqtt.published["stream_state"] == "streaming_off"


def test_the_whole_health_reading_follows_a_stream_state_change_at_once():
    """Seen on both deploys on 2026-09-14: the health thread's first tick ran
    before the first probe, publishing camera_online off and last_frame unknown.
    The probe's `ready` a quarter of a second later corrected stream_state alone,
    so the two contradicted each other for ~35 s until the next tick, and every
    later change -- `stalled` included -- left camera_online lagging the same way."""
    from ios2ha_camera.upstream import Upstream

    class NoIngest:
        def start(self):
            pass

        def stop(self, timeout=None):
            return True

    buf = RingBuffer(5_000_000)
    up = Upstream(NoIngest(), buf, Settings(), probe=lambda: Frame(b"jpeg", time.time()))
    sup = _sup(buf=buf, upstream=up)
    up.on_state = sup.on_stream_state
    sup.publish_state(sup.health_state())      # the first health tick, before any probe
    assert sup.mqtt.published["camera_online"] is False
    up.tick()                                  # the first probe succeeds
    pub = sup.mqtt.published
    assert pub["stream_state"] == "ready"
    assert pub["camera_online"] is True and pub["app_streaming"] is True
    assert pub["last_frame"] is not None


class FakeStarter:
    def __init__(self, result="started"):
        self.calls = []
        self.result = result
        self.state = "idle"
        self.attempts_last_hour = 0

    def attempt(self, manual=False):
        self.calls.append(manual)
        self.state = self.result
        return self.result


class FakeKeyboard:
    def __init__(self, connected=True):
        self._connected = connected

    def connected(self):
        return self._connected

    def state(self):
        return "connected" if self._connected else "advertising"


class FakeOpener:
    def __init__(self, result="opened"):
        self.calls = []
        self.result = result
        self.state = "idle"

    def attempt(self, manual=False):
        self.calls.append(manual)
        self.state = self.result
        return self.result


def test_an_app_that_is_not_running_is_opened():
    """Nothing else here opens the app, so without this a phone that rebooted
    waits for a person however long that takes."""
    op = FakeOpener()
    _sup(phone=FakePhone(running=False), opener=op).phone_state()
    assert op.calls == [False]


def test_an_app_that_is_running_is_not_touched():
    """A cold start kills the running copy: on a suspended app that throws away
    a stream iOS would have woken on its own."""
    op = FakeOpener()
    _sup(phone=FakePhone(running=True), opener=op).phone_state()
    assert op.calls == []


def test_a_cable_that_cannot_answer_does_not_trigger_a_cold_start():
    op = FakeOpener()
    _sup(phone=FakePhone(running_error=PhoneUnavailable("no cable")), opener=op).phone_state()
    assert op.calls == []


def test_a_state_with_nothing_answering_opens_the_app():
    """What a rebooted phone looks like from the network side. Waiting for the
    next five-minute battery sweep would leave the camera dark for no reason."""
    for state in ("backgrounded", "unavailable"):
        op = FakeOpener()
        _sup(opener=op).on_stream_state(state)
        assert op.calls == [False], state


def test_a_state_where_the_app_is_answering_does_not_open_it():
    """`streaming_off` is an app that is open and idle -- the keys' job, not a
    relaunch, which would kill it. The rest are already streaming."""
    op = FakeOpener()
    sup = _sup(opener=op)
    for state in ("streaming_off", "ready", "in_use", "capturing", "stalled"):
        sup.on_stream_state(state)
    assert op.calls == []


class QueueingExecutor:
    """Takes work and never runs it, which is what a blocked cable looks like."""

    def __init__(self):
        self.queued = []

    def submit(self, fn, *a):
        self.queued.append((fn, a))

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass


def test_close_drops_device_actions_still_waiting_for_the_worker():
    """At shutdown only the action already running may finish. The worker is a
    non-daemon thread that the interpreter joins after main() returns, so every
    queued action left behind -- each an ONVIF or cable round trip -- would run
    outside the shutdown budget and push the process past the container's grace
    period into a kill."""
    from concurrent.futures import ThreadPoolExecutor

    ex = ThreadPoolExecutor(max_workers=1)
    sup = _sup(device_executor=ex)
    running, release = threading.Event(), threading.Event()
    ran = []

    def blocking():
        running.set()
        release.wait(5)

    sup._submit("first", blocking)
    assert running.wait(5)
    for i in range(5):
        sup._submit(f"queued_{i}", lambda i=i: ran.append(i))
    sup.close()
    release.set()
    ex.shutdown(wait=True)
    assert ran == []


class GatedPhone(FakePhone):
    """A phone whose first screenshot blocks until released: an action that is
    still running on the device worker."""

    def __init__(self):
        super().__init__()
        self.running, self.release = threading.Event(), threading.Event()
        self.shots = 0

    def screenshot(self):
        self.shots += 1
        self.running.set()
        self.release.wait(5)
        return super().screenshot()


class GatedOnvif(FakeOnvif):
    """ONVIF whose first call blocks until released."""

    def __init__(self):
        super().__init__()
        self.running, self.release = threading.Event(), threading.Event()

    def set_autofocus(self, mode):
        self.running.set()
        self.release.wait(5)
        return super().set_autofocus(mode)


@pytest.mark.parametrize("button", ["screenshot", "relaunch_app", "prepare_phone",
                                    "start_streaming"])
def test_repeated_presses_queue_one_device_action(button, caplog):
    """A misfiring automation or a double-firing dashboard button must not park
    device work ahead of the backgrounding hooks, which share the one worker."""
    ex = QueueingExecutor()
    sup = _sup(phone=FakePhone(), starter=FakeStarter(), device_executor=ex)
    with caplog.at_level("INFO"):
        for _ in range(10):
            sup.handle_control(button, None)
    assert len(ex.queued) == 1
    assert any(button in r.getMessage() and "already" in r.getMessage()
               for r in caplog.records)


def test_a_press_while_the_same_action_runs_is_refused():
    from concurrent.futures import ThreadPoolExecutor

    ex = ThreadPoolExecutor(max_workers=1)
    phone = GatedPhone()
    mq = FakeMqtt()
    sup = _sup(phone=phone, mqtt=mq, device_executor=ex)
    sup.handle_control("screenshot", None)
    assert phone.running.wait(5)
    sup.handle_control("screenshot", None)
    phone.release.set()
    ex.shutdown(wait=True)
    assert phone.shots == 1
    assert len(mq.screenshots) == 1


def test_a_press_after_the_action_finished_runs_again():
    phone = FakePhone()
    mq = FakeMqtt()
    sup = _sup(phone=phone, mqtt=mq)      # Inline: each press completes at once
    sup.handle_control("screenshot", None)
    sup.handle_control("screenshot", None)
    assert len(mq.screenshots) == 2


def test_different_device_actions_do_not_refuse_each_other():
    ex = QueueingExecutor()
    sup = _sup(phone=FakePhone(), device_executor=ex)
    sup.handle_control("screenshot", None)
    sup.handle_control("relaunch_app", None)
    assert len(ex.queued) == 2


def test_a_press_is_not_refused_because_an_automatic_start_is_pending():
    """The automatic attempt honours the pause switch and the rate limit and may
    decline; a person pressing the button overrides both, so it is a different
    request and must not be swallowed by the automatic one."""
    ex = QueueingExecutor()
    starter = FakeStarter()
    sup = _sup(starter=starter, device_executor=ex)
    sup.on_stream_state("streaming_off")
    sup.on_stream_state("streaming_off")
    sup.handle_control("start_streaming", None)
    assert len(ex.queued) == 2
    for fn, a in ex.queued:
        fn(*a)
    assert sorted(starter.calls) == [False, True]


def test_autofocus_changed_while_a_change_is_running_ends_on_the_last_request():
    """A switch carries a value, so a later write is a different request, not a
    repeat: dropping it would leave the camera on the earlier value. Writes made
    while one is running collapse into a single follow-up carrying the latest."""
    from concurrent.futures import ThreadPoolExecutor

    ex = ThreadPoolExecutor(max_workers=1)
    onvif = GatedOnvif()
    sup = _sup(onvif=onvif, device_executor=ex)
    sup.s.autofocus = True
    sup.handle_control("autofocus", False)
    assert onvif.running.wait(5)
    for v in (True, False, True, False):
        sup.handle_control("autofocus", v)
    onvif.release.set()
    # The follow-up is submitted as the first finishes, so shutting the worker
    # down any earlier would refuse it.
    deadline = time.monotonic() + 5
    while len(onvif.calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    ex.shutdown(wait=True)
    assert onvif.calls == ["MANUAL", "MANUAL"]
    assert sup.s.autofocus is False
    assert sup.current_state()["autofocus"] is False


def test_attempts_to_open_the_app_do_not_pile_up():
    """A phone that has just rebooted leaves the first attempt blocked on the
    cable for up to two minutes. Without the guard the health tick would queue
    another every 30 s behind it."""
    ex = QueueingExecutor()
    sup = _sup(opener=FakeOpener(), device_executor=ex)
    for _ in range(5):
        sup.on_stream_state("backgrounded")
    assert len(ex.queued) == 1


def test_the_next_attempt_is_allowed_once_the_last_one_finished():
    op = FakeOpener()
    sup = _sup(opener=op)           # Inline: each attempt completes at once
    sup.on_stream_state("backgrounded")
    sup.on_stream_state("unavailable")
    assert op.calls == [False, False]


def test_a_failed_attempt_does_not_block_every_later_one():
    """The first attempt after a reboot usually raises: the cable is not ready."""
    class Boom:
        state = "unavailable"

        def __init__(self):
            self.calls = 0

        def attempt(self, manual=False):
            self.calls += 1
            raise RuntimeError("no usbmux")

    op = Boom()
    sup = _sup(opener=op)
    sup.on_stream_state("backgrounded")
    sup.on_stream_state("unavailable")
    assert op.calls == 2


def test_the_open_state_is_published():
    op = FakeOpener()
    sup = _sup(phone=FakePhone(running=False), opener=op)
    sup.phone_state()
    assert sup.health_state()["app_open"] == "opened"


def test_without_an_opener_the_open_state_is_still_published():
    """Every declared entity carries a value, or Home Assistant shows it as
    unknown for ever."""
    assert _sup().health_state()["app_open"] == "idle"


def test_streaming_off_runs_the_start_step_once():
    st = FakeStarter()
    sup = _sup(starter=st)
    sup.on_stream_state("streaming_off")
    assert st.calls == [False]


def test_a_state_that_is_not_streaming_off_does_not_run_it():
    """`backgrounded` is a deliberate pause, `unavailable` means the phone is
    gone, and the active states are already streaming. None is fixed by keys."""
    st = FakeStarter()
    sup = _sup(starter=st)
    for state in ("backgrounded", "unavailable", "ready", "in_use", "capturing", "stalled"):
        sup.on_stream_state(state)
    assert st.calls == []


def test_the_button_runs_it_as_a_manual_attempt():
    st = FakeStarter()
    sup = _sup(starter=st)
    sup.handle_control("start_streaming", None)
    assert st.calls == [True]


def test_the_start_state_and_keyboard_are_published():
    sup = _sup(starter=FakeStarter(), keyboard=FakeKeyboard())
    d = sup.health_state()
    assert d["stream_start"] == "idle"
    assert d["stream_start_attempts"] == 0
    assert d["keyboard_connected"] is True


def test_without_a_keyboard_the_values_are_still_published():
    """Every declared entity carries a value, so a missing keyboard reads as
    "no host attached" rather than leaving the entity unknown forever."""
    d = _sup().health_state()
    assert d["keyboard_connected"] is False
    assert d["stream_start"] == "idle"
    assert d["stream_start_attempts"] == 0


def test_phone_state_is_empty_when_the_cable_is_out():
    sup = _sup(phone=FakePhone(error=PhoneUnavailable("no device")))
    assert sup.phone_state() == {}


def test_phone_state_reports_battery_temperature_and_charging():
    sup = _sup(phone=FakePhone())
    assert sup.phone_state() == {
        "battery": 88, "battery_temperature": 27.5, "charging": True,
        "battery_current_ma": 322, "charger_watts": 3, "battery_health_pct": 80.2,
    }


def test_phone_state_reports_the_signed_current_not_just_charging():
    """The phone reports charging while still losing ground on an underpowered
    port, so only the sign of the current says which way it is going."""
    draining = FakePhone(battery={"level": 71, "temperature": 38.9, "charging": True,
                                  "current_ma": -450, "charger_watts": 3, "health_pct": 80.2})
    state = _sup(phone=draining).phone_state()
    assert state["charging"] is True
    assert state["battery_current_ma"] == -450


def test_a_misused_phone_call_is_not_disguised_as_an_unplugged_phone():
    """PhoneManagerMisuseError means the caller wired it onto an event loop.
    Swallowing it would look like a permanently unreachable phone."""
    from ios2ha_camera.phone import PhoneManagerMisuseError

    sup = _sup(phone=FakePhone(error=PhoneManagerMisuseError("called from a running loop")))
    with pytest.raises(PhoneManagerMisuseError):
        sup.phone_state()


def test_publish_controls_covers_every_control():
    sup = _sup()
    sup.publish_controls()
    published = set(sup.mqtt.published)
    assert {c for c in CONTROLS if c != "black_level_override"} <= published


def test_publish_controls_republishes_the_last_still_and_measurements():
    s = Settings()
    s.stacking_mode = "fixed"
    s.fixed_depth = 5
    buf = RingBuffer(5_000_000)
    _fill(buf, 10)
    mq = FakeMqtt()
    sup = _sup(s, buf, mq)
    sup.produce_still(now_fps=12.0)
    before = len(mq.stills)
    sup.publish_controls()
    assert len(mq.stills) == before + 1
    assert "subject_level" in mq.published


def test_current_state_answers_with_everything_published_so_far():
    sup = _sup()
    sup.publish_controls()
    sup.publish_state({"phone_battery": 81})
    state = sup.current_state()
    assert state["phone_battery"] == 81
    assert {c for c in CONTROLS if c != "black_level_override"} <= set(state)


def test_current_state_hands_out_a_copy():
    """Every capture, health and device thread writes the remembered state while
    whoever asked is reading it, so the answer cannot be the live dict."""
    sup = _sup()
    sup.publish_state({"phone_battery": 81})
    taken = sup.current_state()
    taken["phone_battery"] = 0
    taken["invented"] = True
    assert sup.current_state() == {"phone_battery": 81}


def test_a_state_listener_hears_every_publish():
    sup = _sup()
    heard = []
    sup.add_state_listener(heard.append)
    sup.publish_state({"phone_battery": 81})
    sup.publish_state({"exposure": 0.5})
    assert heard == [{"phone_battery": 81}, {"exposure": 0.5}]


def test_a_listener_is_told_only_once_the_value_can_be_read_back():
    """A listener feeding a second interface may be asked for the current state
    the instant it hears a change -- by its own new client, on its own thread.
    Told first, it would answer with the value it has just been told replaced."""
    sup = _sup()
    seen = []
    sup.add_state_listener(lambda d: seen.append(sup.current_state()))
    sup.publish_state({"phone_battery": 81})
    assert seen == [{"phone_battery": 81}]


def test_a_listener_that_raises_costs_only_its_own_events(caplog):
    """These calls arrive on paho's network thread among others, where an
    exception does not merely lose the event: it takes the connection down."""
    mq = FakeMqtt()
    sup = _sup(mqtt=mq)
    second = []

    def broken(d):
        raise RuntimeError("no")

    sup.add_state_listener(broken)
    sup.add_state_listener(second.append)
    with caplog.at_level("ERROR"):
        sup.publish_state({"phone_battery": 81})
    assert mq.published == {"phone_battery": 81}
    assert second == [{"phone_battery": 81}]
    assert sup.current_state() == {"phone_battery": 81}
    assert any("listener" in r.getMessage() for r in caplog.records)


def test_the_reconnect_replay_is_not_repeated_to_listeners():
    """The replay exists because a broker restart drops retained values. A
    listener holding the same values already has them, and sending the lot again
    on every reconnect would be a burst of changes that changed nothing."""
    mq = FakeMqtt()
    sup = _sup(mqtt=mq)
    sup.publish_state({"phone_battery": 81})
    heard = []
    sup.add_state_listener(heard.append)
    before = len(mq.states)
    sup.publish_controls()
    assert len(mq.states) > before + 1          # the controls, then the whole snapshot
    assert len(heard) == 1 and "phone_battery" not in heard[0]


def test_reconnect_republish_survives_concurrent_state_writes():
    """publish_controls runs on the MQTT network thread while capture and
    health threads are still writing. Handing it the live dict raises
    "changed size during iteration" into a caller that only logs, silently
    losing the republish that spec 7 requires on reconnect."""
    class YieldingMqtt(FakeMqtt):
        def publish_state(self, d):
            for _k, _v in d.items():      # iterate like the real client, yielding the GIL
                time.sleep(0)
            super().publish_state(d)

    sup = _sup(mqtt=YieldingMqtt())
    sup.publish_state({f"seed_{i}": i for i in range(200)})
    errors = []
    done = threading.Event()

    def writer():
        # Bounded: every new key changes the dict's size, which is what trips
        # the iteration, but an unbounded writer makes each republish slower
        # than the last.
        try:
            for i in range(3000):
                sup.publish_state({f"late_{i}": i})
                time.sleep(0)
        except Exception as e:           # noqa: BLE001 - the point of the test
            errors.append(e)
        finally:
            done.set()

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    try:
        while not done.is_set():
            sup.publish_controls()
    except Exception as e:               # noqa: BLE001
        errors.append(e)
    finally:
        t.join(timeout=10)
    assert errors == [], errors


def test_a_capture_pipeline_error_is_reported_over_mqtt(monkeypatch):
    """A render or archive failure must not leave every measurement stale
    while the stream still reports healthy."""
    from ios2ha_camera import render

    s = Settings()
    s.stacking_mode = "fixed"
    s.fixed_depth = 4
    buf = RingBuffer(5_000_000)
    _fill(buf, 8)
    mq = FakeMqtt()
    sup = _sup(s, buf, mq)
    monkeypatch.setattr(render, "to_jpeg", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad pixels")))
    ev = sup.produce_still(now_fps=12.0)   # must not raise
    assert "ValueError" in ev["error"] and "bad pixels" in ev["error"]
    assert mq.events[-1] is ev


def test_black_level_echo_reports_the_stored_value_not_the_raw_payload():
    s = Settings()
    sup = _sup(s)
    sup.handle_control("black_level_1", "0.46")
    assert s.black_level_override == (0.0, 0.46, 0.0)
    assert sup.mqtt.published["black_level_1"] == 0.46
    assert isinstance(sup.mqtt.published["black_level_1"], float)


def test_estimate_fps_from_arrival_times():
    buf = RingBuffer(5_000_000)
    now = time.time()
    for i in range(13):
        buf.append(Frame(b"x", ts=now + i * 0.1))
    assert 9.0 < estimate_fps(buf) < 11.0


def test_estimate_fps_falls_back_without_enough_frames():
    assert estimate_fps(RingBuffer(1000)) == 12.0


def test_capture_loop_honours_a_manual_request_without_waiting_for_the_interval():
    s = Settings()
    s.interval_s = 3600
    s.stacking_mode = "fixed"
    s.fixed_depth = 4
    buf = RingBuffer(5_000_000)
    _fill(buf, 8)
    mq = FakeMqtt()
    sup = _sup(s, buf, mq)
    stop = threading.Event()
    t = threading.Thread(target=sup.run_capture, args=(stop,), daemon=True)
    t.start()
    sup.capture_requested.set()
    deadline = time.monotonic() + 5
    while not mq.stills and time.monotonic() < deadline:
        time.sleep(0.02)
    stop.set()
    sup.capture_requested.set()   # wake the loop so it can exit
    t.join(timeout=5)
    assert mq.stills, "a manual capture waited for the 3600 s interval"
    assert not t.is_alive()


class FakeUpstream:
    def __init__(self, state="ready", online=True):
        self._state, self._online = state, online
        self.held = []
        self.session_start = None

    def state(self, now=None):
        return self._state

    def camera_online(self, now=None):
        return self._online

    def holders(self, now=None):
        # Signature must match the real Upstream.holders(now=None): health_state
        # passes the timestamp so holders and state are read at the same instant.
        return ("viewer",) if self._state in ("streaming", "in_use") else ()

    def duty_1h(self, now=None):
        return 12.5

    def tick_age(self, now=None):
        return 0.4

    def session_start_ts(self):
        return self.session_start


def test_health_reports_ready_as_online_and_omits_frame_rate():
    buf = RingBuffer(1 << 20)
    _fill(buf, 3, now=time.time())
    sup = _sup(buf=buf, upstream=FakeUpstream(state="ready"))
    d = sup.health_state()
    assert d["stream_state"] == "ready"
    assert d["camera_online"] is True
    assert "frame_rate" not in d
    assert d["duty_1h"] == 12.5
    assert d["holders"] == "none"


def test_health_reports_frame_rate_while_active():
    """in_use rather than the bare "streaming": the fake reports a viewer
    holding, and the real Upstream names the active sub-state after its holder."""
    buf = RingBuffer(1 << 20)
    _fill(buf, 30, now=time.time())
    sup = _sup(buf=buf, upstream=FakeUpstream(state="in_use"))
    d = sup.health_state()
    assert d["frame_rate"] > 0
    assert d["holders"] == "viewer"


def test_estimate_fps_ignores_frames_from_before_the_session():
    buf = RingBuffer(1 << 20)
    _fill(buf, 5, now=time.time() - 600, step=60)     # probe frames, a minute apart
    _fill(buf, 10, now=time.time(), step=1 / 13)
    assert estimate_fps(buf, since=time.time() - 5) > 10


class RecordingUpstream(FakeUpstream):
    """Records the order of hold release against stacking."""
    def __init__(self, buf, **kw):
        super().__init__(**kw)
        self.buf, self.events = buf, []
        self.session_start = time.time()

    def hold(self, reason, linger_s=0.0):
        outer = self

        class _H:
            def __enter__(self_inner):
                outer.events.append(f"hold:{reason}")
                return self_inner

            def __exit__(self_inner, *exc):
                outer.events.append(f"release:{reason}")
                return False
        return _H()

    def session_start_ts(self):
        return self.session_start

    def wait_for_frames(self, n, since_ts, timeout, poll=0.1):
        return sum(1 for f in self.buf.recent(n) if f.ts >= since_ts)


def test_capture_releases_the_hold_before_it_stacks(monkeypatch):
    buf = RingBuffer(1 << 24)
    up = RecordingUpstream(buf, state="streaming")
    _fill(buf, 20, now=time.time())
    sup = _sup(buf=buf, upstream=up)
    real = stack.stack_frames
    monkeypatch.setattr(stack, "stack_frames",
                        lambda frames: (up.events.append("stack"), real(frames))[1])
    sup.produce_still(now_fps=12.0)
    assert up.events == ["hold:capture", "release:capture", "stack"]


def test_capture_releases_the_hold_when_stacking_setup_raises(monkeypatch):
    buf = RingBuffer(1 << 24)
    up = RecordingUpstream(buf, state="streaming")
    _fill(buf, 20, now=time.time())
    sup = _sup(buf=buf, upstream=up)
    monkeypatch.setattr(stack, "decode", lambda f: (_ for _ in ()).throw(ValueError("truncated")))
    ev = sup.produce_still(now_fps=12.0)
    assert "error" in ev
    assert up.events[-1] == "release:capture"


def test_capture_excludes_a_fresh_frame_from_before_its_session(monkeypatch):
    s = Settings()
    s.stacking_mode = "fixed"
    # Bigger than the buffer's 21 frames, so recent() alone -- with no floor
    # filtering at all -- would already return the stale frame; only the
    # session-start component of floor can be responsible for excluding it.
    s.fixed_depth = 25
    buf = RingBuffer(1 << 24)
    now = time.time()
    _fill(buf, 1, now=now - 20)          # stale, but inside fresh_s=30 and window_max_s=60
    up = RecordingUpstream(buf, state="streaming")
    up.session_start = now - 1
    _fill(buf, 20, now=now)
    sup = _sup(s, buf=buf, upstream=up)
    received = []
    real = stack.stack_frames
    monkeypatch.setattr(stack, "stack_frames",
                        lambda frames: (received.extend(frames), real(frames))[1])
    sup.produce_still(now_fps=12.0, now=now)
    assert received
    assert all(f.ts >= up.session_start for f in received)


def test_capture_with_no_frames_publishes_the_reason():
    buf = RingBuffer(1 << 20)
    up = RecordingUpstream(buf, state="streaming_off")
    up.session_start = time.time()
    sup = _sup(buf=buf, upstream=up)
    ev = sup.produce_still(now_fps=12.0)
    assert ev["error"] == "no frames: streaming_off"


def _jpegs(n, val=30):
    out = []
    for i in range(n):
        rng = np.random.default_rng(i)
        a = np.clip(val + rng.normal(0, 12, (32, 32, 3)), 0, 255).astype("uint8")
        b = io.BytesIO()
        Image.fromarray(a).save(b, "JPEG", quality=92)
        out.append(b.getvalue())
    return out


class StreamingIngestor:
    """Appends frames at the phone's rate from start() until stop(), stamped on
    arrival as the real Ingestor stamps them."""

    def __init__(self, buf, fps=13.9):
        self.buf, self.period, self.jpegs = buf, 1.0 / fps, _jpegs(8)
        self._stop, self._thread = threading.Event(), None

    def start(self):
        stop = self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(stop,), daemon=True)
        self._thread.start()

    def _run(self, stop):
        i = 0
        while not stop.wait(self.period):
            self.buf.append(Frame(self.jpegs[i % len(self.jpegs)], time.time()))
            i += 1

    def stop(self, timeout=None):
        self._stop.set()
        if self._thread is None:
            return True
        self._thread.join(2.0 if timeout is None else timeout)
        return not self._thread.is_alive()


def test_a_capture_that_opens_the_session_measures_only_that_sessions_frames(monkeypatch):
    """Taking the hold only wakes the upstream thread, and the session opens on
    its next tick, so the capture reads session_start_ts() before there is one.
    Falling back to the whole window then counts a 25 s old probe frame, which
    turns 13.9 fps into under 1 and stacks the stale frame with the fresh ones."""
    from ios2ha_camera.upstream import Upstream

    s = Settings()
    s.stacking_mode = "fixed"
    s.fixed_depth = 20
    buf = RingBuffer(1 << 24)
    stale_ts = time.time() - 25
    buf.append(Frame(_jpegs(1)[0], stale_ts))
    ing = StreamingIngestor(buf)
    up = Upstream(ing, buf, s, probe=lambda: None)
    mq = FakeMqtt()
    sup = _sup(s, buf, mq, upstream=up)
    stacked = []
    real = stack.stack_frames
    monkeypatch.setattr(stack, "stack_frames",
                        lambda frames: (stacked.extend(frames), real(frames))[1])
    events = []
    worker = threading.Thread(target=lambda: events.append(sup.produce_still()), daemon=True)
    up.start()
    try:
        worker.start()
        worker.join(timeout=20)
        assert not worker.is_alive(), "the capture did not finish"
        assert "error" not in events[0], events[0]
        assert mq.published["frame_rate"] > 10
        assert stacked and stale_ts not in [f.ts for f in stacked]
    finally:
        up.stop(timeout=5.0)
        ing.stop()


def test_a_capture_during_an_open_session_uses_frames_already_buffered(monkeypatch):
    """A viewer already holds the session, so frames buffered since it opened
    are current and the capture must use them rather than wait for newer ones."""
    from ios2ha_camera import supervisor
    from ios2ha_camera.upstream import Upstream

    class SilentIngestor:
        def start(self):
            pass

        def stop(self, timeout=None):
            return True

    monkeypatch.setattr(supervisor, "CAPTURE_OPEN_TIMEOUT_S", 2.0)
    s = Settings()
    s.stacking_mode = "fixed"
    s.fixed_depth = 20
    buf = RingBuffer(1 << 24)
    now = time.time()
    up = Upstream(SilentIngestor(), buf, s, probe=lambda: None)
    waits = []
    real_wait = up.wait_for_frames

    def timed_wait(*a, **k):
        t = time.monotonic()
        try:
            return real_wait(*a, **k)
        finally:
            waits.append(time.monotonic() - t)

    up.wait_for_frames = timed_wait
    sup = _sup(s, buf, FakeMqtt(), upstream=up)
    viewer = up.hold("viewer")
    try:
        up.tick(now=now - 5)
        _fill(buf, 24, now=now - 0.1, step=1 / 13.9)
        ev = sup.produce_still(now=now)
    finally:
        viewer.release()
    assert "error" not in ev, ev
    assert ev["depth_used"] == 20
    assert waits and max(waits) < 0.5, waits
