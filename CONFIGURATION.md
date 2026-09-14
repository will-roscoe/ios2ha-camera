# Configuration

Everything the supervisor exposes. Environment variables are set once at deploy time; every control
below that is writable live from Home Assistant and takes effect on the next capture.

## Environment

Read by `Settings.from_env`, and coerced to the type of each field's default.

| Variable | Default | Notes |
|---|---|---|
| `PHONE_HOST` | `192.168.1.11` | The phone must have a fixed address: the camera app binds to whatever IP it had at launch |
| `PHONE_PORT` | `8080` | |
| `PHONE_PASSWORD` | — | HTTP Basic for `/mjpeg` and `/snapshot.jpg`; WS-Security digest for `/onvif/*` |
| `PHONE_BUNDLE_ID` | `com.guardianeye.webcam.ios` | Used by the relaunch button |
| `MQTT_HOST` / `MQTT_PORT` | `192.168.1.5` / `1883` | |
| `MQTT_USER` / `MQTT_PASSWORD` | — | A broker-local login, such as the Mosquitto add-on's `logins` option. No Home Assistant account is needed; see OPERATIONS.md |
| `MQTT_PREFIX` | `ios2ha_camera` | Root of every topic |
| `ARCHIVE_DIR` | `/data/archive` | Inside the container |
| `LIVE_PORT` | `8099` | `/live` and `/still.jpg` |
| `BUFFER_BYTES` | `1073741824` | Ring buffer budget. Compressed frames, so 1 GB is several minutes |
| `LOG_LEVEL` | `INFO` | |

Secrets belong in the deployment `.env`, never in the repository.

## Which knob do I actually want?

Three questions come up repeatedly, so they are worth answering directly.

**"The image is too dark."** Two different controls, and picking the wrong one wastes capture time:

- If the image is **clean but gloomy**, raise **`night_key`**. It is the photographic key of the
  render — free, applied to data you already have.
- If the shadows are **grainy**, raise **`target_level`**. That gathers more signal, but depth goes
  as the *square* of it: 45 → 90 is four times the frames, four times the capture duration and four
  times the heat.

**"Expose for the dark part of the scene."** Lower **`subject_percentile`**. "Subject" is not a
region — it is a percentile of the whole-frame luminance histogram after black-level subtraction.
Lower tracks the darkest parts and stacks harder; higher tracks midtones and stacks less. There is
no spatial region selection.

**"Control the focal point."** You cannot. Focus mode is the only imaging control that works, and
**turning autofocus off makes things worse** — see [OPERATIONS.md](OPERATIONS.md#what-the-phone-will-and-will-not-do).

## Controls

All are `entity_category: config`, so Home Assistant files them under Configuration.

### Numbers

| Entity | Name | Range | Unit | Default |
|---|---|---|---|---|
| `number.ios2ha_camera_interval_s` | Capture interval | 30–3600 step 1 | s | `600` |
| `number.ios2ha_camera_target_level` | Target subject level | 1–255 step 1 | | `45.0` |
| `number.ios2ha_camera_subject_percentile` | Subject percentile | 1–50 step 1 | % | `10.0` |
| `number.ios2ha_camera_clean_factor` | Clean factor k | 1–64 step 1 | | `4.0` |
| `number.ios2ha_camera_fixed_depth` | Fixed depth | 1–1000 step 1 | frames | `30` |
| `number.ios2ha_camera_max_depth` | Maximum depth | 1–2000 step 1 | frames | `600` |
| `number.ios2ha_camera_window_max_s` | Maximum capture window | 1–120 step 1 | s | `60.0` |
| `number.ios2ha_camera_ramp_tau_s` | Ramp time constant | 0–7200 step 10 | s | `900.0` |
| `number.ios2ha_camera_night_key` | Night key | 0.01–0.5 step 0.01 | | `0.09` |
| `number.ios2ha_camera_stretch_strength` | Stretch strength | 1–60 step 1 | | `25.0` |
| `number.ios2ha_camera_chroma_radius` | Chroma smoothing radius | 0–12 step 0.5 | px | `3.0` |
| `number.ios2ha_camera_denoise_strength` | Denoise strength | 0–20 step 0.5 | | `3.0` |
| `number.ios2ha_camera_black_level_0` | Black level override R | 0–32 step 0.1 | | auto |
| `number.ios2ha_camera_black_level_1` | Black level override G | 0–32 step 0.1 | | auto |
| `number.ios2ha_camera_black_level_2` | Black level override B | 0–32 step 0.1 | | auto |
| `number.ios2ha_camera_live_max_window_s` | Stacked feed window | 0.5–15 step 0.5 | s | `3.0` |
| `number.ios2ha_camera_background_below_pct` | Background below | 0–100 step 1 | % | `50` |
| `number.ios2ha_camera_resume_above_pct` | Resume above | 0–100 step 1 | % | `80` |

What the less obvious ones do:

- **Clean factor k** scales the whole depth law: how clean the result has to be. Depth is `k·G²`,
  so it is a linear multiplier on capture cost.
- **Maximum capture window** bounds how far back in time a stack may reach, so moving things smear
  less. Depth is capped at `fps × window_max` regardless of `max_depth`.
- **Ramp time constant** smooths depth across intervals. At the default 900 s, dusk raises depth
  over about fifteen minutes rather than jumping.
- **Black level overrides** are blank until you set one, which means "estimate it per stack" — the
  right default, because a fixed value tints the still (two runs measured 0.47/0.37/0.51 and
  0.24/0.46/0.21). Blank is not zero; setting one to 0 pins it to zero.
- **Denoise strength** is conservative on purpose: at higher strengths it sharpens text but paints
  flat areas into mottling.
- **Background below / Resume above** are a hysteresis band, not two independent settings. The app
  is backgrounded between captures once the battery falls below the first, and left alone once it
  rises above the second; in between, whatever it was doing continues. A single threshold would let
  a phone resting near it flip on every reading, and each flip costs a usbmux round trip. Set the
  wrong way round (resume below background) the feature disables itself and logs a warning.
- **Stacked feed window** caps how many seconds of frames `/live/stacked` may blend into its running
  average — a smear budget, not a frame count, so it means the same thing whatever the phone's frame
  rate happens to be. It follows the same brightness law as the stills, just capped lower.

### Selects and switches

| Entity | Options | Default |
|---|---|---|
| `select.ios2ha_camera_stacking_mode` | `off` (single frame), `fixed`, `auto` | `auto` |
| `select.ios2ha_camera_white_balance` | `as-shot`, `grey-world`, `brightest-patch` | `as-shot` |
| `select.ios2ha_camera_render_intent` | `natural`, `inspection` | `natural` |
| `switch.ios2ha_camera_stacking_allowed` | Forces depth 1 when off, whatever the mode | on |
| `switch.ios2ha_camera_autofocus` | **Leave this on** | on |
| `switch.ios2ha_camera_on_demand` | Off restores the old always-connected behaviour: the phone connection is held open for the life of the process instead of opened per capture or viewer | on |
| `switch.ios2ha_camera_backgrounding` | Allows the app to be backgrounded between captures when the battery is low, which is what lets the phone actually charge. Turning it off while the app is backgrounded foregrounds it immediately | on |

`grey-world` is offered but not recommended: a frame dominated by one colour pushes it into a false
cast. `brightest-patch` is the closest alternative to as-shot for object colour.

### Buttons

| Entity | What it does |
|---|---|
| `button.ios2ha_camera_capture_now` | Requests a capture without shifting the schedule |
| `button.ios2ha_camera_screenshot` | Phone screenshot → `camera.ios2ha_camera_screenshot` |
| `button.ios2ha_camera_relaunch_app` | Relaunches the camera app over USB. **Leaves streaming off** — someone must tap Start streaming — so this is for a dead app only |
| `button.ios2ha_camera_prepare_phone` | Runs the same steps as `./deploy.sh phone` (lockdown, Developer Mode check, developer image mount, relaunch, battery) over USB, reported as an event. Never enables Developer Mode itself — that reboots the phone, so only `--fix-developer-mode` on the command line can — and never waits for someone to tap Start streaming |

## Sensors

Luminance figures are relative, 0–255 after black-level subtraction. They are **not lux**: absolute
illuminance would need exposure and gain metadata the app does not provide.

### The scene

| Entity | Unit | Meaning |
|---|---|---|
| `sensor.ios2ha_camera_subject_level` | | The percentile the depth law aims at |
| `sensor.ios2ha_camera_lum_mean` | | Mean luminance |
| `sensor.ios2ha_camera_lum_p1` … `_p99` | | 1st, 10th, 50th, 90th, 99th percentiles |
| `sensor.ios2ha_camera_dynamic_range` | | 99.5th over 0.5th percentile, before black-level subtraction |
| `sensor.ios2ha_camera_highlight_clip` | % | Share of pixels with any channel saturated |
| `sensor.ios2ha_camera_colour_temperature` | K | Of the rendered scene's remaining cast, not the light source |
| `sensor.ios2ha_camera_motion` | | Mean absolute difference between consecutive probe frames |
| `sensor.ios2ha_camera_focus_sharpness` | | Variance of the Laplacian; falls if focus drifts |
| `sensor.ios2ha_camera_noise` / `_snr` | | From the dark region of the finished still |

### The capture

| Entity | Unit | Meaning |
|---|---|---|
| `sensor.ios2ha_camera_depth_used` | frames | Frames actually stacked |
| `sensor.ios2ha_camera_depth_required` | frames | Before clamping — shows when limits rather than light are setting depth |
| `sensor.ios2ha_camera_capture_duration` | s | |
| `sensor.ios2ha_camera_effective_exposure_required` / `_achieved` | s | Depth ÷ frame rate. An upper bound until auto-exposure has hit its limit |
| `sensor.ios2ha_camera_wb_gain_r/g/b` | | The gains actually applied |
| `sensor.ios2ha_camera_black_level_applied_r/g/b` | | The black level actually applied |

### Health and hardware

| Entity | Unit | Meaning |
|---|---|---|
| `binary_sensor.ios2ha_camera_camera_online` | | Active with a frame within 30 s, **or** `ready` with a frame within 150 s; forced off whenever the upstream thread's own heartbeat is more than 180 s stale, regardless of state. Deliberately **false while `backgrounded`**: nothing yet brings the app back to the foreground, so frames really are unavailable |
| `binary_sensor.ios2ha_camera_reachable_over_wifi` | | The phone's IP stack answered. True even when `backgrounded`, because a refusal proves something is there; false only on a timeout or no route |
| `binary_sensor.ios2ha_camera_reachable_over_cable` | | usbmuxd is answering and a device is attached. Follows `cable_state` being `ok` |
| `binary_sensor.ios2ha_camera_app_running` | | The camera app's process is alive, read over the cable. **The only thing that separates a backgrounded app from one iOS has killed** — over WiFi both simply refuse. Refreshed on the battery cadence, not the health tick, and **unknown** rather than false when the cable cannot answer |
| `binary_sensor.ios2ha_camera_app_streaming` | | Reachable *and* willing to send. False for `backgrounded` and for `streaming_off` |
| `binary_sensor.ios2ha_camera_capturing_stills` | | A capture is holding the connection |
| `binary_sensor.ios2ha_camera_being_viewed` | | A viewer is holding it, including during the 30 s linger — the connection is still open and still costing battery |
| `binary_sensor.ios2ha_camera_backgrounding_active` | | The power-saving policy is engaged, i.e. the battery is below the threshold and the app is being backgrounded between captures. Not the same as the `backgrounding` switch, which only says it is *allowed*: with a full battery the switch is on and this is off |
| `sensor.ios2ha_camera_stream_state` | | What the camera is doing, in one value. `unavailable` (nothing answers: off the network, or timed out); `backgrounded` (the port refuses, so the phone answers but the app is not listening); `streaming_off` (the app answered 503 — open, but Start streaming was never tapped); `ready` (reachable and willing, just not connected to); `capturing` (frames flowing for a still); `in_use` (frames flowing for a viewer, which wins when both apply); `streaming` (frames flowing with nobody holding, only reachable with `on_demand` off) |
| `sensor.ios2ha_camera_cable_state` | | The USB side, independent of the WiFi stream above. `ok`; `no_device` (usbmuxd is answering, the cable is out — ordinary); `usbmuxd_down` (the daemon is unreachable and the socket mount is presumed orphaned — a fault, and the one worth alerting on). See OPERATIONS.md |
| `sensor.ios2ha_camera_last_frame` | | Timestamp, rendered as a live relative age |
| `sensor.ios2ha_camera_frame_rate` | fps | Measured from arrival times; 11–14.5 in practice |
| `sensor.ios2ha_camera_holders` | | Why the phone is connected right now: `capture`, `viewer`, `viewer (linger)` while a viewer's 30 s grace period runs down, `on_demand_off` when the switch above is off, or `none` |
| `sensor.ios2ha_camera_live_fps` | fps | Output rate of `/live/stacked`, not the phone's own frame rate |
| `sensor.ios2ha_camera_duty_1h` | % | Share of the last hour the phone connection was actually open |
| `sensor.ios2ha_camera_tick_age` | s | Time since the upstream thread's own loop last ran; stuck here means the thread is wedged, not just the phone |
| `sensor.ios2ha_camera_battery` | % | |
| `sensor.ios2ha_camera_battery_current_ma` | mA | **Signed.** Negative means the load exceeds the supply — the one figure `charging` cannot give you |
| `sensor.ios2ha_camera_charger_watts` | W | What the port advertises |
| `sensor.ios2ha_camera_battery_health_pct` | % | Maximum against design capacity |
| `sensor.ios2ha_camera_battery_temperature` | °C | |
| `binary_sensor.ios2ha_camera_charging` | | Connected, which is not the same as gaining charge |
| `sensor.ios2ha_camera_archive_free_gb` | GB | Archiving stops below 2 GB |

## Internal constants

Values that shape the on-demand connection and the live feed but are not exposed as controls,
because each is either derived from another constant, a measured device limit, or a policy decision
that only makes sense read alongside the code and comments that justify it — not a dial a dashboard
user has a reason to turn blind.

| Constant | Module | Value | Why it isn't a dashboard control |
|---|---|---|---|
| `PROBE_INTERVAL_S` | `upstream.py` | `60.0` s | How often the idle probe runs. `ONLINE_IDLE_S` and the backoff ladder are both derived from it, so changing it alone would desynchronise them from what they assume |
| `PROBE_BACKOFF_MAX_S` | `upstream.py` | `300.0` s | Ceiling on the probe backoff after repeated failures (60, 120, 240, then capped here). Trades "how often to knock on a wedged app" against "how fast a recovery is noticed" — a call best made reading the code, not turning a dial |
| `ONLINE_IDLE_S` | `upstream.py` | `150.0` s | How old the newest frame may be before `ready` gives up and falls back to `unavailable`. Sized off `PROBE_INTERVAL_S` and the probe's own read timeout, so it isn't independent of them |
| `CAPTURE_OPEN_TIMEOUT_S` | `upstream.py` | `20.0` s | How long a capture's first wait for frames may take after taking its hold, before it gives up with "no frames". Sized with headroom over the measured ~113 ms first-frame latency, not something to tune per capture |
| `TICK_STALE_S` | `upstream.py` | `180.0` s | How stale the upstream thread's own heartbeat may get before `camera_online` is forced false regardless of state. A safety net for a dead thread, not a normal operating parameter |
| `VIEWER_LINGER_S` | `live.py` | `30.0` s | How long the phone stays connected after the last `/live` or `/live/stacked` viewer leaves, so a quick reconnect doesn't retrigger a probe. A connection-lifecycle policy, not a per-viewer preference |
| `WRITE_TIMEOUT_S` | `live.py` | `30.0` s | How long a stalled write may run before a viewer is treated as gone. Chosen from measurement — 5 s wrongly dropped real viewers under about 1.2 Mbit/s — so it is already tuned to the actual failure mode, not a knob a viewer session should override |
| `FPS_CAP` | `live.py` | `12` | Rate limit on the broadcast loop. Above the phone's own ~13.9 fps it would only add CPU, never real frames |
| `QUEUE_SIZE` | `live.py` | `2` | Depth of each viewer's frame queue. Short on purpose — a client behind by more than this is meant to be dropped, not buffered |
| `SCALE` | `live_stack.py` | `2` | `/live/stacked`'s downscale factor (1920×1080 → 960×540), applied via the JPEG decoder's own DCT scaling. Full resolution measured 4 fps and was never offered |
| `REFRESH_S` | `live_stack.py` | `2.0` s | How often tone parameters and depth are re-estimated from a pixel subsample. A CPU-versus-staleness trade-off already tuned to the cost of estimation, not a viewing preference |
| `GAP_RESET_S` | `live_stack.py` | `2.0` s | A gap this long between frames resets the running average instead of blending across it — reflects the streaming cadence, not a stylistic choice |
| `JPEG_QUALITY` | `live_stack.py` | `85` | Output quality for `/live/stacked`, lower than the stills' default of 92 because it runs at video rate, not once every ten minutes |
| `NIGHT_GAIN` | `render.py` | `4.0` | The brightening gain at which the day/night blend is fully the night render. Reads alongside the reason it exists -- the phone's JPEGs are already tone-mapped, so a scene needing no brightening should keep the camera's own look -- not a look a dashboard number can substitute for |

## Topics

| Topic | Payload |
|---|---|
| `ios2ha_camera/still` | Latest display JPEG, retained |
| `ios2ha_camera/screenshot` | Phone screenshot PNG, retained |
| `ios2ha_camera/state/<name>` | One per sensor, retained |
| `ios2ha_camera/event` | Per still: `ts`, `depth_used`, `depth_requested`, `short`, `subject_level`, `path`, `archived`. Errors carry `error` |
| `ios2ha_camera/available` | `online` / `offline`, retained, last-will |
| `ios2ha_camera/set/<name>` | Control writes |

Automations triggering on `ios2ha_camera/event` should test `archived` — the topic also carries
error events, and snapshotting on one of those writes a stale frame.
