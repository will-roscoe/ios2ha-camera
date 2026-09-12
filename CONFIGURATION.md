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
| `MQTT_USER` / `MQTT_PASSWORD` | — | A dedicated non-admin Home Assistant account; the Mosquitto add-on authenticates HA users |
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

### Selects and switches

| Entity | Options | Default |
|---|---|---|
| `select.ios2ha_camera_stacking_mode` | `off` (single frame), `fixed`, `auto` | `auto` |
| `select.ios2ha_camera_white_balance` | `as-shot`, `grey-world`, `brightest-patch` | `as-shot` |
| `select.ios2ha_camera_render_intent` | `natural`, `inspection` | `natural` |
| `switch.ios2ha_camera_stacking_allowed` | Forces depth 1 when off, whatever the mode | on |
| `switch.ios2ha_camera_autofocus` | **Leave this on** | on |

`grey-world` is offered but not recommended: a frame dominated by one colour pushes it into a false
cast. `brightest-patch` is the closest alternative to as-shot for object colour.

### Buttons

| Entity | What it does |
|---|---|
| `button.ios2ha_camera_capture_now` | Requests a capture without shifting the schedule |
| `button.ios2ha_camera_screenshot` | Phone screenshot → `camera.ios2ha_camera_screenshot` |
| `button.ios2ha_camera_relaunch_app` | Relaunches the camera app over USB. **Leaves streaming off** — someone must tap Start streaming — so this is for a dead app only |

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
| `binary_sensor.ios2ha_camera_camera_online` | | Streaming **and** a frame within 30 s |
| `sensor.ios2ha_camera_stream_state` | | `streaming`, `streaming_off` (app answered 503), `unreachable` |
| `sensor.ios2ha_camera_last_frame` | | Timestamp, rendered as a live relative age |
| `sensor.ios2ha_camera_frame_rate` | fps | Measured from arrival times; 11–14.5 in practice |
| `sensor.ios2ha_camera_battery` | % | |
| `sensor.ios2ha_camera_battery_current_ma` | mA | **Signed.** Negative means the load exceeds the supply — the one figure `charging` cannot give you |
| `sensor.ios2ha_camera_charger_watts` | W | What the port advertises |
| `sensor.ios2ha_camera_battery_health_pct` | % | Maximum against design capacity |
| `sensor.ios2ha_camera_battery_temperature` | °C | |
| `binary_sensor.ios2ha_camera_charging` | | Connected, which is not the same as gaining charge |
| `sensor.ios2ha_camera_archive_free_gb` | GB | Archiving stops below 2 GB |

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
