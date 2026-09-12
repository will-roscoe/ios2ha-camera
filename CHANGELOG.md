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
