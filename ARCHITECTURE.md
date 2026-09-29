# Architecture

One process, one connection to the phone opened on demand, a fixed roster of background threads,
and an event loop. This document covers what each module does and — more usefully — the decisions
that are not obvious from reading the code.

## Threads

This is the part most worth understanding before changing anything.

| Thread | Runs | Why it is separate |
|---|---|---|
| **upstream** | Decides *when* the phone is connected: opens and closes the one MJPEG connection on a 0.5 s tick, and probes the app every 60 s while nothing holds it | Every connect, disconnect, linger expiry and probe must happen in one place, or two callers could race the connection — the thing that wedges the phone |
| **ingest** | One MJPEG connection, filling the ring buffer, for as long as `upstream` wants it open | Must never be blocked by anything downstream; only alive for the life of a session, not the process |
| **live-stack** | Re-derives the low-light tone curve over the newest buffered frame at video rate | Only runs while `/live/stacked` has a viewer; started and stopped from an aiohttp handler, so it must never block that handler |
| **capture** | The interval loop: stack, render, publish, archive | A night stack takes tens of seconds |
| **health** | A 30 s tick publishing stream state, frame time and, every 5 min, the battery | Health on the capture interval would take ten minutes to report an outage |
| **device worker** | One thread for ONVIF and USB actions triggered from MQTT | See below |
| **paho network** | The MQTT client's own loop | Not ours to block |
| **aiohttp loop** | The live view, and only the live view | Serves `/live`, `/live/stacked` and `/still.jpg` |

Three rules hold this together:

- **Nothing may raise into paho's network thread.** `suppress_exceptions` defaults to `False`
  (verified in paho 2.1.0, `client.py:1595`), so an uncaught exception in a callback tears down the
  connection and the supervisor goes deaf to every later control. `handle_control` therefore never
  raises, and the whole `_on_message` body — including the payload decode — is inside the guard.
- **Nothing slow may run on that thread either.** A `capture_now` press only sets an `Event`; the
  capture thread does the work. Stacking there would blow past the 30 s broker keepalive.
- **Nothing may call `PhoneManager` from the event loop.** Its methods block, and they raise
  `PhoneManagerMisuseError` — deliberately not `PhoneUnavailable` — if called from inside a running
  loop, so that wiring mistake cannot masquerade as an unplugged phone.

Shared state is small and guarded: `_last_state` (written from several threads — capture, health,
the device worker, paho's network thread, and now also `ingest` and `upstream` via
`on_stream_state` — and read whole on reconnect) lives under a lock, and the reconnect republish
sends a snapshot. Handing over the live
dict raised `RuntimeError: dictionary changed size during iteration` into a caller that only logged
it, which silently disabled the republish — the failure mode that gave the lock its reason to exist.

## Modules

### `settings.py`
Every runtime value in one dataclass, with environment overrides coerced to the type of each
field's default (ports arriving as strings would otherwise reach aiohttp and paho as `str`).
`CONTROLS` is the set of names Home Assistant may write; `apply_control` rejects a value outside a
select's options rather than silently falling back mid-render, and accepts `black_level_0/1/2` as
per-channel writes into one tuple.

### `ring_buffer.py`
A byte-bounded FIFO of **compressed** frames. Decoded, a 1080×1920 frame is about 6 MB; compressed
it is 140–450 KB, so a 1 GB budget holds several minutes of history. `recent(n)` slices with
`islice` from a clamped start — the obvious `list(dq)[-n:]` returns the *entire* buffer when `n` is
zero.

### `mjpeg.py`
Parses `multipart/x-mixed-replace` into JPEG payloads. Parts are cut on their declared
`Content-Length`, and a part that is not a whole JPEG is dropped so the stream resyncs on the next
boundary. The tempting alternative — scanning for JPEG start/end markers — silently truncates any
frame carrying an EXIF thumbnail, because the thumbnail is itself a complete JPEG. The buffer is
capped, so a stream that never completes a part cannot grow memory without bound.

### `ingest.py`
One persistent `GET /mjpeg`, and the module where the "never churn" rule lives:

- `streaming` is reported on the **first frame**, not on HTTP 200 — a frozen app that answers and
  then sends nothing would otherwise read as healthy forever.
- Every reconnect backs off, **including after a clean end of stream**. Reconnecting immediately
  made 2698 connections in 1.5 seconds against a stream that closes early. The backoff resets only
  after a connection that actually stayed up.
- Connect and read have separate timeouts: a phone asleep at the TCP level blocks in `connect()`,
  where there is no socket for `stop()` to shut down.
- `start()` joins a previous thread first, so a restart can never leave two clients on the phone.

Its lifetime is no longer the process's: `upstream.py` calls `start()`/`stop()` around each session, so
this module still owns *how* to stream but no longer owns *when*.

### `upstream.py`
Decides *when* the phone is connected. `Ingestor` still decides how.

An open connection costs about 300 mA continuously and idle costs roughly nothing, so the
connection is a resource taken and given back rather than held for the process's life. Anything
that wants frames calls `hold(reason, linger_s=...)`, which returns a `Hold` — a capture's hold has
no linger (it releases the instant it has its frames), a viewer's lingers for `VIEWER_LINGER_S` so a
quick reconnect doesn't retrigger a probe. Releasing a hold twice is harmless by design: a viewer
that drops mid-write can reach the release path more than once, and a second decrement must not
cancel someone else's hold.

Every decision — opening, closing, expiring a linger, running a probe — happens on one thread
(`tick()`, run every 0.5 s), so two callers can never race the connection. Taking or releasing a
hold only touches a counter under a lock, which is why `live.py` can do it directly from the aiohttp
event loop without blocking it.

**While idle**, the thread probes the app every `PROBE_INTERVAL_S` (60 s): connect, read one frame,
close — 82–387 ms and about 2–3 mA a minute. A probe's frame lands in the ring buffer like any other,
which is what lets `idle` still report a recent frame time. Consecutive failures back off — 60, 120,
240 s, capped at `PROBE_BACKOFF_MAX_S` (300 s) — rather than knocking on a wedged app every minute
for as long as it stays broken.

**The frame-floor rule.** A capture takes a hold, then computes
`floor = max(session_start_ts() or now, now - window_max_s)` and only stacks frames at or after it.
`session_start_ts()` is `None`, or the timestamp the *current* session opened at — never a probe's.
Without the floor, a capture starting just as the connection opens would see the idle probe's last
frame, up to a minute old, sitting in the buffer beside its own, and could stack a minute-old
snapshot in with fresh frames. A session already open when the capture starts (a viewer is
watching) keeps its earlier `session_start_ts()`, so the capture uses whatever has already been
buffered rather than waiting for a fresh session. The hold is released — and the connection can
close — before stacking and rendering run, which is 10–20 s of CPU the phone no longer has to stay
connected for.

**Getting the phone back, in three parts.** Bringing the phone back — cable, app running, app in
front — is done over the USB cable by the flows that already exist (`Upstream` sessions,
`Backgrounding.foreground`, the relaunch button, the cable self-heal). *Making the app present* when
none of those has, which is what a restarted phone needs, is `app_open.AppOpener`: it cold starts the
app when the phone says it is not running, and brings it to the front when it is running but
answering nothing, never acting on a cable that cannot answer and never while the battery policy has
deliberately backgrounded the app. *Starting the stream* is a third
separate step, `stream_start.StreamStarter`, because Guardian Eye only starts streaming from a
button on its own screen. That step drives `keyboard.Keyboard`, a Bluetooth LE keyboard the host
emulates through BlueZ, which iOS Full Keyboard Access acts on. It is kept narrow on purpose: it
presses keys only when the app is proven open and idle (its server answers 503), and re-checks that
immediately before the activating press, since the same key stops a running stream. Nothing in
either module knows the phone's address or name, so the phone can be swapped without configuration.

**The state model.** `state()` returns one of these, most specific first (the full list of values
and what each means lives in `states.py`):

- `stalled` — the last session or probe got a frame and then timed out: the app's capture has hung
  with its HTTP server still up. Checked before freshness, because a hung app hands every reconnect
  one frame, so there is always a frame younger than `FRESH_S`. Ingest only reports `streaming` on a
  connection's *second* frame and the probe reads two, for the same reason.
- `streaming` (or `capturing` / `in_use`, by who holds it) — the *current* session has produced a frame no older than `FRESH_S` (30 s), and that
  frame is newer than the session's own start. A stale probe frame from before the session opened
  cannot satisfy this, so a session that has only ever failed cannot read as streaming on the
  strength of someone else's frame.
- `streaming_off` / `backgrounded` / `unavailable` — the last session or probe ended in that failure.
- `ready` — no failure, and *some* frame (a probe's, most likely) is newer than `ONLINE_IDLE_S`
  (150 s — roughly two probe intervals plus the probe's own read timeout, so one missed probe does
  not flip this early).
- `unreachable` — otherwise.

`camera_online()` is `True` for `streaming` or `idle`, *unless* the thread's own heartbeat
(`tick_age()`) has gone stale for more than `TICK_STALE_S` (180 s), in which case it is `False`
regardless of `state()`. A dead or wedged upstream thread must not leave `camera_online` frozen at
whatever it last read as healthy — the exact silent-failure mode the offline alert exists to catch.

`holders()` names why the phone is connected — `capture`, `viewer`, `viewer (linger)` while a linger
is running down, `on_demand_off` when the switch disables on-demand entirely, or `()` (published as
`none`) when nothing is holding it and it is merely idle-probing or shut. `duty_1h()` sums open
spans over the trailing hour, including the one in progress, as a percentage.

### `live_stack.py`
Serves `/live/stacked`: the stills' low-light treatment at a rate a shared box can hold.

**Why not `render.render`.** That renderer measured 1.9 s a frame at full resolution with its
defaults, and 424 ms with denoise and chroma blur off — against a budget of about 80 ms a frame at
`FPS_CAP`. `LiveStacker` keeps the same tone arithmetic (`_tone_natural`'s, reimplemented as
`tone()`) but buys the speed back three ways: half resolution via the JPEG decoder's own DCT scaling
(`SCALE = 2`, 1920×1080 → 960×540 — full resolution measured 4 fps and is not offered), an O(1)
running mean instead of decoding and stacking N frames, and tone parameters estimated from a
sixteenth of the pixels (`SUBSAMPLE = 4`) every `REFRESH_S` (2 s) rather than every frame. Measured
against `render.render`'s output, the difference is at most 2/255. It reimplements only
`_tone_natural`'s arithmetic, so `/live/stacked` always renders the `natural` intent regardless of
`render_intent` — there is no fast-path equivalent of `inspection`'s asinh stretch here, and
inspection is a still-only diagnostic tool.

The running mean is `acc += (new - acc) * a`, with `a = max(1/t, 1/N)`: a plain running mean for the
first `N` frames, settling into an exponential moving average once `t` passes `N`, so older frames
are down-weighted rather than remembered forever. `N` (`depth()`) follows the same brightness law as
the stills (`stack.choose_depth`), but with `hard_cap` set from `live_max_window_s` (default 3 s) in
frames at the live frame rate, rather than the stills' 600-frame night cap — the live feed's depth
means seconds of smear, not a fixed frame budget.

The background thread only exists while at least one viewer is attached: `attach()`/`detach()` are
called from an aiohttp handler and must never block it, so a still-exiting predecessor thread (from
a previous viewer generation) is joined inside the *new* thread, not in `attach()` itself. A fresh
`threading.Event` per generation, rather than reusing one, is what stops a `detach()` racing a
new `attach()` from having its `set()` wiped out by the next generation's `clear()`.

### `measure.py`
Pure functions over decoded frames: per-channel black level (a low percentile), luminance
percentiles, colour temperature (sRGB → CIE xy → McCamy), motion, focus sharpness, and the
pre-clamp depth requirement. One `luminance()` definition is shared by the mean, the percentiles
and the subject level so they cannot drift apart.

### `stack.py`
Sigma-clipped mean — **never a median**: in low light the subject sits at 0–2 out of 255 per frame
and a median posterises it into flat patches.

Frames stream into additive per-pixel accumulators (sum, sum of squares in float64, count), one
decoded frame at a time, with a bounded number in flight. Memory is therefore independent of depth.
Decoding every frame first needed about **15 GB** at the 600-frame night depth on a host with 9 GB
free; measured peak is now ~28× one frame instead of ~2185×. Decode runs on a **thread** pool, not
the spec's process pool: JPEG decode releases the GIL (measured 4.6× on four threads) and threads
avoid pickling 50 MB accumulators and forking a process that already runs other threads.

**The depth law.** With subject level `S` and target `T`:

```
gain  G = max(T / S, 1)
depth N = clamp( ceil(k · G²), 1, min(N_max, fps · window_max) )
```

Noise falls with the square root of frame count, so brightening by `G` at constant noise needs
about `G²` frames. `N` is then smoothed across intervals by an EMA, so dusk raises depth gradually
and a passing cloud does not make it flap. There is no luminance threshold anywhere in the system.

### `render.py`
Black level (estimated per stack, or overridden), white balance, then a tone map. Every curve
derives **one** scale factor from luminance and multiplies all three channels by it, which is what
keeps hue intact — looking the curve up per channel compresses each differently and shifts colour.
`natural` is photographic tone reproduction keyed to geometric-mean luminance, low key, with darker
tones progressively desaturated the way eyes lose colour in dim light. `inspection` is an asinh
stretch to near full range for reading detail, and is meant to look processed.

`calibration()` returns the black level and gains that `render()` will apply, and `render()` accepts
them back, so the sensors reporting those numbers cannot drift from the image they describe.

**The day/night blend.** `natural`'s curve assumes it is given linear light, but the phone's JPEGs
are already gamma-encoded and tone-mapped by iOS -- running `natural` on them unconditionally is a
double tone map, and in daylight it shows up as flat, desaturated output. `render()` blends
`natural`'s result back towards the stacked mean as shot, by `night_weight()`: the same brightening
gain the depth law already computes (`G = target_level / subject`, floored at 1) also sets how much
of the night render to use, `w = clip((G - 1) / (NIGHT_GAIN - 1), 0, 1)`. A scene bright enough to
need no brightening (`G <= 1`) keeps exactly the camera's own rendering; a scene needing `NIGHT_GAIN`
or more (`4.0`, the ceiling the night look was tuned against) is byte-for-byte what `render()`
produced before this blend existed, so the night look is preserved by construction rather than by
retuning it alongside the daylight fix. Chroma smoothing and denoise scale with the same weight, so a
daylight still is not softened on top of needing no tone map. `inspection` ignores the blend outright
-- it exists to read detail, not to look natural. `live_stack.py`'s `tone()` applies the identical
blend, from a weight `refresh_params` derives from the same kind of subject measurement on its own
subsample, against its own accumulated mean as shot.

### `live.py`
An aiohttp app serving three routes. `/live` re-serves the buffered MJPEG; `/live/stacked` re-serves
`LiveStacker`'s noise-reduced feed the same way; `/still.jpg` returns the last stacked still from
memory, falling back to the newest raw frame.

A background broadcaster per source reads its latest frame at a capped rate (`FPS_CAP`, 12) and fans
it out to a short (`QUEUE_SIZE`, 2) per-client queue with non-blocking puts, dropping the oldest
frame for a client that has fallen behind. The fan-out never awaits a client socket, so a stalled
viewer can stall only its own request — structurally, not by convention. That is the exact failure
that killed the previous system, so it is the property the tests are built around.

A connected `/live` or `/live/stacked` viewer now also holds the phone connection open, via
`Upstream.hold("viewer", linger_s=VIEWER_LINGER_S)` — that is what makes the connection on demand
rather than permanent. Which makes a stalled client a new problem: it used to only stall its own
request, but now it would also pin the phone at 300 mA for as long as it stayed connected, and
aiohttp has no write timeout to end that. So every write is bounded by `WRITE_TIMEOUT_S` (30 s), and
a write that times out is treated exactly like a viewer that left. 30 s rather than a shorter figure:
at 5 s this also disconnected steady readers below roughly 1.2 Mbit/s (measured on loopback), because
the kernel only wakes a paused writer once much of the send buffer has drained — a genuinely slow
viewer, not a stalled one. `/live/stacked` additionally attaches/detaches `LiveStacker`'s background
thread to the same connected lifetime.

`/still.jpg` never touches the phone and takes no hold at all, so a dashboard refreshing a thumbnail
costs the phone nothing — which is also what makes an on-demand upstream possible in the first
place: if the thumbnail route held the phone, it would never go idle.

### `onvif.py`
A small WS-Security SOAP client. The `/onvif/*` services need UsernameToken PasswordDigest —
`base64(sha1(nonce + created + password))` — while the media endpoints take plain HTTP Basic. Only
focus is exposed — the mode and an absolute lens position read back with `GetStatus` — because it is
the only imaging control proven to do anything. The supervisor treats focus as a setting to keep,
not a command to send once: it is checked against the phone at the start of every session and put
back if it has drifted, because the app starts on autofocus every time it is relaunched.

### `focus.py`
The service's own autofocus, as a pure search over a `measure(position)` callable: `score` is the
edge energy of the most detailed quarter of an averaged half-size luma, border excluded; `search`
probes 1.00 to 0.60 far to near, brackets the far-most peak within 10% of the best and golden-section
refines it. The supervisor supplies `measure` -- move the lens over ONVIF, gather frames after it
settles, average, score -- and runs it on the capture thread (`Supervisor.run_focus`).

### `colour_ref.py`
Holds night stills to a picked region's remembered colour. `record` keeps the region's median colour
and a small log-luma picture of the whole view; `gains` registers the whole view by phase correlation
(its shift only -- its confidence was no guide at night), checks the aligned views still correlate,
measures the region where the view says it went, and returns gains normalised so brightness is
untouched. The whole view decides where the region went because matching the region locally locked
onto a look-alike block 90 px away on real data.

### `horizon.py`
Levels stills. `estimate_tilt` takes line segments (OpenCV's LSD) within 10 degrees of vertical and
fits lean = roll + k * x to them, robust and weighted by length: a camera pitched up fans its
verticals out in proportion to their distance from the centre, so the roll is the lean at the
centre. Rooflines are not used -- each facade's leant 3 to 8 degrees by perspective on real stills. A
view with too few verticals, a loose fit or an uncertain roll raises `NoClearHorizon`. `level` turns a
picture back about its centre and zooms just enough that no corner shows. The supervisor turns the
stack after the flat field (fixed to the sensor's pixels) and before white balance and the colour
reference (whose region is drawn on the levelled still).

### `flat.py`
The sensor's fixed grain. `check` accepts only a recording that is lit above the black clamp, below
white, and free of edges (so the uncovered view is refused); `pattern` is the fine texture at a
two-pixel scale; `FlatField.correct` subtracts it at a scale fitted per still and per channel over
the least textured 40% of the picture, and returns the share of grain left, which the renderer uses
to scale its smoothing. Night stills only, by `night_weight`.

### `control_store.py`
Keeps the control values Home Assistant writes in `CONTROLS_FILE`, so a restart does not reset them —
and every phone reboot restarts the service, through the usbmuxd hook. Only control names are read
back, each through `Settings.apply_control`, the validation a live write gets, so the file cannot
reach a deployment setting. Written beside and renamed over, so a crash mid-write keeps the old file.

### `phone.py`
pymobiledevice3 wrappers over the Lightning cable, written against the 11.x API, which is `async`
throughout. Every public method blocks and is safe with the cable out. The three DVT-backed calls
mount the developer disk image on demand — it does not survive a phone reboot — lazily, treating
"already mounted" as success and retrying after a failure. `battery()` deliberately never mounts:
it is a plain lockdown service, so it keeps working on a freshly rebooted phone.

`prepare()` runs lockdown, Developer Mode, the developer image mount, an app launch and a battery
read as five independent steps, each recorded even if an earlier one failed — "the developer image
is missing" and "the app is not running" are separate facts, and whoever is reading this after a
phone restart wants both. It never turns Developer Mode on itself: that is the one step
`enable_developer_mode()` performs, and only when asked explicitly, because it reboots the phone.

### `phone_setup.py`
The CLI behind `./deploy.sh phone`: `python -m ios2ha_camera.phone_setup`. Runs `PhoneManager.prepare()`
(with `--fix-developer-mode` first, if given — again, only on request, because of the reboot), then,
unless `--no-wait`, polls `upstream.http_probe` every 5 s for up to `--timeout` (default 300 s)
seconds, printing what changed once per kind of failure rather than once per poll, since a wait that
can never succeed (a wrong password, the app gone) must not look identical to one still waiting for
someone to tap Start streaming. It shares `http_probe` with `upstream.py` rather than rolling its own
probe, so "does the app answer with a frame" is asked the same way everywhere it's asked.

### `archive.py`
Writes the display JPEG under `YYYY/MM/DD/HHMMSS.jpg` via a temporary file and an atomic rename, so
a reader never sees a half-written frame, and reports free space.

### `entities.py`
Every entity the service publishes, declared once: object id, domain, display name, unit, device
class, options, range and the state key it reads. `mqtt_io` renders these into Home Assistant
discovery payloads, and a Home Assistant custom component in its own repository will read the same
declaration, so the two cannot drift apart as they would have done with a hand-kept copy each. It
also carries `MEDIA`, the video and image the HTTP server offers, where `holds_phone` records which
of them keep the phone's connection open while a viewer is attached.

### `mqtt_io.py`
The MQTT client, and the renderer that turns `entities.ENTITIES` into Home Assistant discovery
payloads -- one small builder per domain, so a declared entity of a kind nothing renders raises
rather than being silently left unpublished. State is **retained**, so Home Assistant
shows real values immediately after its own restart rather than "unknown" until the next interval,
and an `on_connected` callback republishes controls, measurements and the last still on every
reconnect. `connect_async` means a broker that is down at startup does not stop capture. `close()`
publishes "offline" explicitly, because a clean disconnect does not fire the last will.

Every entity pins its `entity_id` with `default_entity_id`. Without it Home Assistant derives the id
from the device and entity *display names*, so renaming anything would silently break every
automation and dashboard pointing at it.

### `supervisor.py`
The interval loop and the wiring. Beyond the obvious:

- **Stale frames never become a still.** With no frame newer than 30 s it publishes an event and
  returns, rather than archiving a picture of the past as the present.
- Frames older than the capture window are excluded, and the event carries `depth_used`,
  `depth_requested` and `short`. With an `Upstream` configured, "older than the capture window" is
  the frame-floor rule described under `upstream.py`: a capture takes a hold, then only ever stacks
  frames at or after `max(session_start_ts(), now - window_max_s)`, so a 60-second-old idle-probe
  frame from before its session opened can never enter the stack.
- **Archiving stops below a free-space floor** while the MQTT still keeps publishing.
- Noise is measured from a high-pass residual in the darkest tenth of the finished still, with a
  median absolute deviation. Whole-image standard deviation — the obvious choice — rises with scene
  detail and would read as the camera getting noisier whenever something interesting appeared.
- Dynamic range is computed *before* black-level subtraction, because afterwards the 0.5th
  percentile is zero by construction: the black level **is** the 0.5th percentile.

### `__main__.py`
Reads the environment, builds everything, starts the threads, and hands the main thread to the live
server. `web.run_app` returns on SIGTERM, which is how the container stops the process.

**The shutdown budget.** Compose allows 30 s from SIGTERM before it SIGKILLs. `run_app` is given
`shutdown_timeout=2.0`, but a `/live` viewer waiting for a frame can sit out that timeout *twice* —
once while aiohttp waits for handlers to finish, once again after cancelling them — measured 4.0 s,
not 2. Then `up.stop(timeout=5.0)`, `keyboard.stop(timeout=3.0)`, two 3 s thread joins (capture,
health) and up to 2 s for `mqtt.close()`: 4 + 5 + 3 + 3 + 3 + 2 = 20 s, leaving 10 s of
headroom. Each term is a worst case and they have not been seen together. The grace period used
to be 20 s, which left no headroom at all. A device action already running when the stop arrives is waited for after `main()`
returns, outside this sum; queued ones are dropped. An upstream
thread still alive after its 5 s is presumed stuck inside a probe's own timeout, and `stop()`
deliberately leaves it alone rather than touching the ingestor from a second thread, which is the
kind of two-client churn that wedges the app. Measured end to end with a viewer attached: 1.1–5.1 s
— against 122 s before this branch, when the container's 20 s grace period was exceeded and SIGKILL
landed before the offline availability was ever published.

## Testing

222 tests, no hardware. `tests/fakes/fake_camera.py` is a real HTTP server that serves real JPEGs
with chunked transfer encoding and HTTP Basic, and can answer 503 or drop the connection, so the
ingest path is exercised over real sockets rather than mocks.

Several tests exist because a mutation check proved they catch a specific regression: the
Content-Length parser against a frame with an embedded thumbnail, the reconnect backoff against a
tight loop, the state race against the pre-fix republish. Where a test's whole value is catching one
mistake, that is worth preserving.
