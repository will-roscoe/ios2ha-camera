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

    def shutdown(self, wait=True):
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
    return Supervisor(s, buf or RingBuffer(5_000_000), mqtt or FakeMqtt(),
                      archive or FakeArchive(), device_executor=Inline(), **kw)


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


def test_screenshot_publishes_a_png():
    sup = _sup(phone=FakePhone())
    sup.handle_control("screenshot", "PRESS")
    assert sup.mqtt.screenshots and sup.mqtt.screenshots[0].startswith(b"\x89PNG")


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
    def __init__(self, state="idle", online=True):
        self._state, self._online = state, online
        self.held = []
        self.session_start = None

    def state(self, now=None):
        return self._state

    def camera_online(self, now=None):
        return self._online

    def holders(self):
        return ("viewer",) if self._state == "streaming" else ()

    def duty_1h(self, now=None):
        return 12.5

    def tick_age(self, now=None):
        return 0.4

    def session_start_ts(self):
        return self.session_start


def test_health_reports_idle_as_online_and_omits_frame_rate():
    buf = RingBuffer(1 << 20)
    _fill(buf, 3, now=time.time())
    sup = _sup(buf=buf, upstream=FakeUpstream(state="idle"))
    d = sup.health_state()
    assert d["stream_state"] == "idle"
    assert d["camera_online"] is True
    assert "frame_rate" not in d
    assert d["duty_1h"] == 12.5
    assert d["holders"] == "none"


def test_health_reports_frame_rate_while_streaming():
    buf = RingBuffer(1 << 20)
    _fill(buf, 30, now=time.time())
    sup = _sup(buf=buf, upstream=FakeUpstream(state="streaming"))
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


def test_capture_ignores_frames_from_before_its_session():
    buf = RingBuffer(1 << 24)
    now = time.time()
    _fill(buf, 4, now=now - 120, step=60)          # probe frames
    up = RecordingUpstream(buf, state="streaming")
    up.session_start = now - 1
    _fill(buf, 20, now=now)
    sup = _sup(buf=buf, upstream=up)
    ev = sup.produce_still(now_fps=12.0, now=now)
    assert ev["depth_used"] <= 20


def test_capture_with_no_frames_publishes_the_reason():
    buf = RingBuffer(1 << 20)
    up = RecordingUpstream(buf, state="streaming_off")
    up.session_start = time.time()
    sup = _sup(buf=buf, upstream=up)
    ev = sup.produce_still(now_fps=12.0)
    assert ev["error"] == "no frames: streaming_off"
