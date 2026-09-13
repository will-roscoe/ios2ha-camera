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
