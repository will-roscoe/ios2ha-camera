# Changelog

## What the version numbers mean

The **Home Assistant entity and topic surface is the public interface**, because that is what other
things depend on: automations, dashboards and templates all name entities directly. So:

- **MAJOR** — an existing entity id, topic or payload shape changes or disappears. Something that
  was working will stop working, and you will have to go and edit automations.
- **MINOR** — new entities, topics or capabilities. Nothing that already worked breaks.
- **PATCH** — fixes and internal changes with no visible surface change.

Renaming an entity's *display name* is a patch: entity ids are pinned with `default_entity_id`
precisely so that display names can be improved without breaking anything.

The running version is visible in three places: the first line of the container log, the
`sw_version` on the Home Assistant device page, and `./deploy.sh version`. It carries the git
description as well as the number, so an image built from a modified tree reads as `1.0.0-dirty`
rather than passing as a clean release.

## 2.3.1 — 2026-09-14

**`camera_online` no longer contradicts `stream_state` after a change.** A stream state change
published `stream_state` alone, leaving `camera_online`, the booleans and `last_frame` at whatever
the previous health tick computed, for up to 30 seconds. It showed after every restart: the first
health tick runs before the first probe, so for about 35 seconds Home Assistant read `ready`
alongside `camera_online` off and `last_frame` unknown. A state change now publishes the whole
health reading with it. No entity or payload changes.

## 2.3.0 — 2026-09-14

**`stream_state` has a new value, `stalled`: the app's picture froze while it still answers.** On
14 September Guardian Eye's capture hung with its HTTP server still up — a black screen and a loading
ring in the app — so every connection got one frame and then silence. For ten minutes Home Assistant
showed nothing wrong at all: `stream_state` read `ready` and `capturing`, `camera_online` stayed on,
and stills were stacked from 5 and then 2 frames instead of 600. Two checks were each fooled by the
single frame:

- **The idle probe read one frame and closed**, so it passed every minute and kept `ready` fed. It now
  reads two, which costs one more frame interval (about 70 ms).
- **A session counted any frame under 30 s old as streaming**, and reconnecting every ~10 s always
  produced one. The stall is now checked first, and the stream only reports `streaming` once a
  connection delivers its second frame.

`stalled` makes `camera_online` false, so the existing offline alert fires. `reachable_over_wifi`
stays true and `app_streaming` is false. Prepare phone no longer says "frames are flowing" for a
frozen app. A stalled connection also no longer resets the reconnect backoff, which had been knocking
on the hung app every ten seconds.

**Add a `stalled` branch to the offline alert's message** if you use the template: the fix is to stop
and start streaming in the app, which is not what `unavailable` tells you to do. OPERATIONS.md has the
updated template. Nothing else changes, so this is a minor release; see OPERATIONS.md for the one
downgrade step a new enum option needs.

## 2.2.0 — 2026-09-14

**`MQTT_URL`: one setting for the broker, with fallbacks.** The scheme carries the transport, TLS and
default port together, so there is no matrix of port, TLS and transport variables to keep consistent:

```
MQTT_URL='mqtt://192.168.1.5'                    # plain TCP, port 1883
MQTT_URL='mqtts://broker.lan'                    # TLS, port 8883
MQTT_URL='ws://192.168.1.5:1884'                 # websockets
MQTT_URL='wss://broker.example/mqtt'             # websockets over TLS
MQTT_URL='mqtts://broker.lan mqtts://backup.lan' # tried in order
```

When a connection attempt fails, the next broker in the list is tried, wrapping round. That uses
paho's own `on_connect_fail` hook and its host and port setters, which are only permitted while
disconnected — exactly when the hook runs.

Two things are refused at startup rather than failing later and more confusingly:

- **Credentials in the URL.** They belong in `MQTT_USER` and `MQTT_PASSWORD`. A URL carrying them would
  be quoted by any error or log line about it, so the error names the entry by position instead.
- **A list that mixes schemes**, such as `mqtt://` with `wss://`. paho fixes the transport when the
  client is created and TLS before it connects, so a fallback cannot move between them.

**Nothing breaks.** `MQTT_HOST` and `MQTT_PORT` are deprecated but keep working when `MQTT_URL` is unset,
without logging anything, since a deployment that has not migrated is not a fault. Setting both is the
only case that warns, naming the variables `MQTT_URL` overrode.

## 2.1.0 — 2026-09-14

**The phone now charges instead of merely holding station.** When the battery is low the
supervisor backgrounds the camera app between captures and wakes it again when something
needs frames. Measured on the device: backgrounded, the phone went from **30% to 96% in
under three hours**, where streaming on the same 3 W port barely breaks even.

iOS suspends a backgrounded app's listening socket while leaving the process running, so
this is a pause rather than a stop. Verified on the device across a full round trip, with
the process id unchanged at every step and the stream resuming with no tap:

| Step | pid | Stream |
|---|---|---|
| before | 22711 | streaming, 186,939 B |
| backgrounded | 22711 | connection refused within 3 s |
| foregrounded | 22711 | streaming, 185,601 B |

**Hysteresis, not a threshold.** It engages below `background_below_pct` (default 50%) and
releases above `resume_above_pct` (default 80%), holding whatever it was doing in between.
One threshold would let a phone resting near it flip on every reading, and each flip costs a
usbmux round trip and drops the WiFi radio into power save. If the two numbers are set the
wrong way round the feature disables itself and says so, rather than thrashing.

**New controls:** `switch.ios2ha_camera_backgrounding` (default on),
`number.ios2ha_camera_background_below_pct`, `number.ios2ha_camera_resume_above_pct`.

**New sensor:** `binary_sensor.ios2ha_camera_backgrounding_active` — whether the policy is
currently engaged, which is not the same as the switch being on. With a full battery the
switch is on and this is off.

**The offline alert had to change with it**, and this is the reason 2.0.0 came first. A
backgrounded app reports `stream_state: backgrounded`, which is offline as far as
`camera_online` is concerned — correct for a phone that wandered into the background on its
own, since nothing will bring it back. But a backgrounding *this service performed* is not a
fault: the app wakes for the next capture by itself. So `camera_online` now stays true in
that one case, and only that one — a phone somebody else backgrounded still alerts, and so
does a genuine outage while the policy is engaged.

Turning the switch off while the app is backgrounded foregrounds it immediately, rather than
leaving the camera dark until the battery happened to recover.

Also fixed: `PhoneManager.launch` never passed `kill_existing`, and pymobiledevice3 defaults
it to `True`. Every "launch" therefore killed the running app and cold-started a new one, and
a cold start leaves streaming off until somebody taps Start streaming in the app. That is
right for the relaunch button, which exists to revive a dead app, so `launch()` keeps it;
`foreground()` and `background()` are new and do not.

## 2.0.0 — 2026-09-13

**`stream_state`'s values have changed. Automations and templates that match them by
name need editing.** An existing payload's shape changed, which by the rule at the top of
this file is a major version, so this is 2.0.0. Migration table below.

"The camera is not sending frames" used to be one word covering four unrelated situations:
a dead cable, a phone off the network, an app iOS had suspended, and an app whose Start
streaming button was never tapped. Only two of those are faults, and they need different
fixes. The distinction turned out to be free — measured on the device on 13 September,
a backgrounded app **refuses** the connection immediately (iOS suspends the listening
socket but leaves the process alive, and the phone still answers ICMP at 70–149 ms), while
a phone genuinely off the network **times out**. The probe already makes that connection;
it just had to stop flattening both into one state.

| Was | Now | Why |
|---|---|---|
| `unreachable` | `unavailable` | Nothing answered: off the network, or the connection timed out |
| `unreachable` | `backgrounded` | The port refused, so the phone answered and the app is not listening |
| `idle` | `ready` | Reachable and willing, just not connected to at the moment |
| `streaming_off` | `streaming_off` | Unchanged. The app is open but Start streaming was never tapped, and only a person can fix that |
| `streaming` | `capturing` | Frames are flowing for a still |
| `streaming` | `in_use` | Frames are flowing for a viewer. Wins over `capturing` when both apply |
| `streaming` | `streaming` | Frames flowing with nobody holding, which happens only when `on_demand` is off |

**Eight new boolean sensors**, because `stream_state` answers "what is it doing" while
these answer "which part is broken":

`reachable_over_cable`, `reachable_over_wifi`, `app_running`, `app_streaming`,
`capturing_stills`, `being_viewed`, alongside the existing `camera_online` and `charging`.

They are independent axes rather than a decomposition of the enum: the cable and the WiFi
fail separately, and the app can be alive on a phone that is sending nothing.

`app_running` is the one that needs the cable. Over WiFi a suspended app and one iOS has
killed both simply refuse, so only a `proclist` over usbmux separates them. It is read on
the battery cadence rather than the health tick, because it is a usbmux round trip, and it
publishes **unknown** rather than `false` when the cable cannot answer — a false "app not
running" would send someone to relaunch a perfectly healthy app.

**The offline alert needs updating**, and OPERATIONS.md carries the new template.
`camera_online` deliberately stays false while backgrounded: nothing in this release brings
the app back to the foreground, so frames really are unavailable. 2.1.0, which backgrounds
the app on purpose, is what makes that alert need to change — which is why this release
comes first.

## 1.1.2 — 2026-09-13

The cable stopped working for two hours on 13 September and nothing noticed. usbmuxd had
restarted at 17:10; the container bind-mounts its socket as a *file*, so the container kept
holding the old, deleted inode and every battery reading and app check failed with
`ConnectionFailedToUsbmuxdError` until the container was recreated by hand. usbmuxd exits
whenever no device is attached, so every unplug can do this.

Two fixes, because the good one needs root and cannot ship in the image.

**On the host**, `scripts/install-usbmuxd-hook.sh` installs an `ExecStartPost` drop-in on
`usbmuxd.service` that restarts the container as soon as usbmuxd comes back. This is the
real fix and it acts in seconds. It is guarded: it runs through `systemd-run --no-block` so
usbmuxd's own startup is never held up, it restarts only a container that is already
running, it exits quietly when Docker is not up yet, and it rate-limits to one restart a
minute because udev starts usbmuxd on every attach and a cable with a bad contact must not
restart the container in a loop.

**In the container**, a fallback for hosts without that hook: after usbmuxd has been
unreachable for five minutes, the supervisor asks itself to exit and Docker's restart policy
brings it back on a fresh socket. It is deliberately slow and heavily guarded, because a
service that restarts itself on a wrong signal is worse than the fault it fixes. It never
fires in the first ten minutes of a process (a cable already broken at boot would otherwise
loop forever), never twice within an hour, and never for a phone that is merely unplugged —
that raises `NoDeviceConnectedError`, which means usbmuxd is answering fine.

**New entity:** `sensor.ios2ha_camera_cable_state`, with values `ok`, `no_device` and
`usbmuxd_down`. This makes the fault alertable long before the restart is reached, and
distinguishes a cable that is simply out from a daemon that has gone. Adding an entity is a
minor change by the rule above; this is released as a patch because nothing that already
worked behaves differently, and the entity exists to report a fault that previously had no
signal at all.

## 1.1.1 — 2026-09-13

`natural`'s tone curve was tuned for night and assumed its input was linear light, but the phone's
JPEGs already carry iOS's own gamma encoding and tone mapping -- applying `natural`'s Reinhard curve
and gamma on top of that a second time made daylight stills flat and desaturated. Measured on a real
960×540 daylight frame: the camera's own frame has saturation 38.1 and RMS contrast 58.1; the
pre-1.1.1 render gave 18.4 and 54.7, with shadows lifted from 45 to 66.

The render now blends between the camera's own look and the night render, by how much brightening
the scene needs -- the same measurement that already sets the stack depth. A scene bright enough to
need none of it (daylight) now keeps exactly the camera's own rendering, cleaned up by stacking; a
scene needing four times brightening or more (night) is byte-for-byte what the render produced
before this release; everything between blends smoothly. `/live/stacked` blends the same way,
against its own running mean. `inspection` is unaffected -- it exists to read detail, not to look
natural.

No entities, topics or payload shapes changed.

234 tests, no hardware required.

## 1.1.0 — 2026-09-13

The phone connection is now on demand rather than held open for the life of the process. Streaming
continuously cost about 300 mA and flattened the phone in around six hours on this installation's 3 W
port; the connection is now something a capture or a live viewer takes and gives back, and while
nothing needs it the phone is probed every 60 seconds instead — about 2–3 mA a minute. A capture
releases the connection before it stacks and renders, so the 10–20 seconds of CPU that takes no
longer keeps the phone connected for nothing. A dedicated thread makes every connect and disconnect
decision, so nothing can race the connection the way two clients used to be able to.

A new `/live/stacked` route serves the stills' low-light tone curve as a live feed at 960×540, built
from a running average that only runs while someone is actually watching it.

Shutdown is now bounded well inside the container's grace period even with a viewer attached —
measured 1.1–5.1 s, against 122 s before, when the grace period could be exceeded and the offline
state never got published.

`./deploy.sh phone` brings the phone back after a restart: checks lockdown and Developer Mode,
mounts the developer disk image, relaunches the app, and waits for someone to tap Start streaming,
confirming frames actually arrive. `--fix-developer-mode` turns Developer Mode on by hand when
needed — it reboots the phone, so nothing does that automatically.

**Home Assistant surface:** 7 entities added, none removed or renamed — `switch.ios2ha_camera_on_demand`
(off restores the previous always-connected behaviour), `number.ios2ha_camera_live_max_window_s`,
`button.ios2ha_camera_prepare_phone`, and diagnostics `sensor.ios2ha_camera_holders`,
`sensor.ios2ha_camera_live_fps`, `sensor.ios2ha_camera_duty_1h` and `sensor.ios2ha_camera_tick_age`.
`sensor.ios2ha_camera_stream_state` gains a new option, `idle`, for a phone that is connected on
demand but not currently streaming to anyone.

222 tests, no hardware required.

## 1.0.0 — 2026-09-12

First deployed version. Replaces an arrangement in which Home Assistant pulled MJPEG directly from
the phone; a stalled viewer froze the phone's HTTP server, exhausted its kernel mbufs and panicked
it, and the timelapse stopped for eleven days without anyone noticing.

A supervisor is now the only client of the phone. It holds one MJPEG connection, buffers compressed
frames, and every ten minutes stacks a set of them into a still whose depth rises as the light
falls, renders it, publishes it over MQTT and archives it. It also re-serves the live stream so a
slow viewer can never reach the phone, drives ONVIF focus, reads the phone's battery over USB, and
reports its own health.

**Home Assistant surface:** 62 entities under one device — two cameras, 34 sensors, two binary
sensors, 15 numbers, three selects, two switches and three buttons — each with a pinned entity id,
a readable name, and units, device classes and state classes where they apply. Topics are under
`ios2ha_camera/`, with retained state, a `still_ready` event per capture and a last-will
availability topic.

Verified against the real device before release: stills stacking and archiving, ONVIF autofocus in
both directions, battery and temperature over USB, the live feed re-served, and the offline alert
firing five minutes after the camera app was closed.

157 tests, no hardware required.
