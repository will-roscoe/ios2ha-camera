# Architecture

One process, one connection to the phone, five threads and an event loop. This document covers what
each module does and — more usefully — the decisions that are not obvious from reading the code.

## Threads

This is the part most worth understanding before changing anything.

| Thread | Runs | Why it is separate |
|---|---|---|
| **ingest** | One persistent MJPEG connection, filling the ring buffer | Must never be blocked by anything downstream |
| **capture** | The interval loop: stack, render, publish, archive | A night stack takes tens of seconds |
| **health** | A 30 s tick publishing stream state, frame time and, every 5 min, the battery | Health on the capture interval would take ten minutes to report an outage |
| **device worker** | One thread for ONVIF and USB actions triggered from MQTT | See below |
| **paho network** | The MQTT client's own loop | Not ours to block |
| **aiohttp loop** | The live view, and only the live view | Serves `/live` and `/still.jpg` |

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

Shared state is small and guarded: `_last_state` (written from four threads, read whole on
reconnect) lives under a lock, and the reconnect republish sends a snapshot. Handing over the live
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

### `live.py`
An aiohttp app serving two routes. `/live` re-serves the buffered MJPEG; `/still.jpg` returns the
last stacked still from memory, falling back to the newest raw frame.

A background broadcaster reads the ring buffer at a capped rate and fans each frame out to a short
per-client queue with non-blocking puts, dropping the oldest frame for a client that has fallen
behind. The fan-out never awaits a client socket, so a stalled viewer can stall only its own
request — structurally, not by convention. That is the exact failure that killed the previous
system, so it is the property the tests are built around.

`/still.jpg` never touches the phone, so a dashboard refreshing a thumbnail costs the phone nothing.

### `onvif.py`
A small WS-Security SOAP client. The `/onvif/*` services need UsernameToken PasswordDigest —
`base64(sha1(nonce + created + password))` — while the media endpoints take plain HTTP Basic. Only
focus **mode** is exposed, because it is the only imaging control proven to do anything.

### `phone.py`
pymobiledevice3 wrappers over the Lightning cable, written against the 11.x API, which is `async`
throughout. Every public method blocks and is safe with the cable out. The three DVT-backed calls
mount the developer disk image on demand — it does not survive a phone reboot — lazily, treating
"already mounted" as success and retrying after a failure. `battery()` deliberately never mounts:
it is a plain lockdown service, so it keeps working on a freshly rebooted phone.

### `archive.py`
Writes the display JPEG under `YYYY/MM/DD/HHMMSS.jpg` via a temporary file and an atomic rename, so
a reader never sees a half-written frame, and reports free space.

### `mqtt_io.py`
The MQTT client and the Home Assistant discovery payloads. State is **retained**, so Home Assistant
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
  `depth_requested` and `short`.
- **Archiving stops below a free-space floor** while the MQTT still keeps publishing.
- Noise is measured from a high-pass residual in the darkest tenth of the finished still, with a
  median absolute deviation. Whole-image standard deviation — the obvious choice — rises with scene
  detail and would read as the camera getting noisier whenever something interesting appeared.
- Dynamic range is computed *before* black-level subtraction, because afterwards the 0.5th
  percentile is zero by construction: the black level **is** the 0.5th percentile.

### `__main__.py`
Reads the environment, builds everything, starts the threads, and hands the main thread to the live
server. On SIGTERM it stops ingest, joins the loops and publishes "offline" before disconnecting.

## Testing

157 tests, no hardware. `tests/fakes/fake_camera.py` is a real HTTP server that serves real JPEGs
with chunked transfer encoding and HTTP Basic, and can answer 503 or drop the connection, so the
ingest path is exercised over real sockets rather than mocks.

Several tests exist because a mutation check proved they catch a specific regression: the
Content-Length parser against a frame with an embedded thumbnail, the reconnect backoff against a
tight loop, the state race against the pre-fix republish. Where a test's whole value is catching one
mistake, that is worth preserving.
