# Connecting over MQTT

## You do not need a Home Assistant account

The ios2ha-camera service authenticates to the **MQTT broker**, not to Home
Assistant. Home Assistant's own MQTT integration is what turns the messages it
publishes into entities, but the login the service uses to get onto the broker
in the first place is a broker account, unrelated to any Home Assistant user —
and none is needed.

Use a broker-local login. If you're running the Mosquitto add-on, its `logins`
option takes a list of username/password pairs that exist only in the broker,
not in Home Assistant's own user database:

```yaml
logins:
  - username: ios2ha
    password: change-me
```

Any other broker works the same way through its own password file —
`mosquitto_passwd` for a standalone Mosquitto, for instance — and the broker
does not have to be the one Home Assistant itself uses. Anywhere both Home
Assistant and the service can reach is enough.

One thing worth knowing before you pick a password: add-on logins are stored
as **plain text** in the add-on's own options, readable by anything with
access to Home Assistant's Supervisor API. Give this account its own password,
used nowhere else, rather than reusing one that matters.

### Narrowing the account with an ACL

A login added through `logins` can publish and subscribe to anything on the
broker. This account only ever needs its own topic prefix plus one slice of
Home Assistant's discovery namespace, so it is worth narrowing: turn on the
add-on's `customize.active` option and point it at an ACL file placed under
`/share/mosquitto`.

The service publishes and subscribes under exactly two prefixes: everything
under `ios2ha_camera/` — state, the still and screenshot images, events,
availability, and the `ios2ha_camera/set/` topics it listens on for control
writes (the full list is in {doc}`../supervisor/configuration`) — and the
retained discovery messages it publishes at
`homeassistant/<domain>/ios2ha/<object id>/config`, one per entity. It never
subscribes to anything of Home Assistant's own, including its status topic, so
the account needs no read access there:

```
user ios2ha
topic readwrite ios2ha_camera/#
topic write homeassistant/+/ios2ha/+/config
```

The `ios2ha` segment in the discovery topic is fixed in the code, not the
`MQTT_PREFIX` described below — it stays the same whatever prefix you
configure.

## Configuring the service

Four environment variables cover the MQTT side; the full table, including the
deprecated `MQTT_HOST`/`MQTT_PORT` pair, is in {doc}`../supervisor/configuration`:

- `MQTT_URL` — the broker, as a `mqtt://`, `mqtts://`, `ws://` or `wss://` URL.
  The scheme sets the transport, TLS and default port together. Space-separate
  more than one URL for a fallback list.
- `MQTT_USER` / `MQTT_PASSWORD` — the broker-local login above.
- `MQTT_PREFIX` — root of every topic other than discovery; defaults to
  `ios2ha_camera`.
- `EXPOSE_MQTT` — turns discovery and state publishing on or off; defaults to
  `true`.

With none of these set, the service looks for a broker at `mqtt://localhost` —
a broker on the same machine, which is what a plain Mosquitto add-on install
gives you once the container itself runs with host networking.

## What appears in Home Assistant

Once the service reaches the broker, every control and sensor it publishes is
discovered automatically and grouped under one device — nothing to add by
hand beyond the camera entries below. See {doc}`entities` for the full list of
what gets created.

Their entity ids are pinned rather than derived from display names, so
renaming an entity later does not move automations and dashboards onto a new
id; see {doc}`../guarantees` for what that promise actually covers.

## Adding the camera entities

MQTT discovery covers every control and sensor, but not the camera feeds
themselves — a Home Assistant `camera` entity backed by an MJPEG stream is not
something MQTT discovery can create, so these are added once by hand, under
Settings → Devices & services → Add integration → **Generic Camera**.

The service serves three fixed paths on `LIVE_PORT` (default `8099`):

- `/live` — the raw MJPEG stream, forwarded live from the phone
- `/live/stacked` — the same view, noise-reduced by stacking successive frames,
  which is worth more than the raw stream in low light
- `/still.jpg` — a single JPEG, with no stream held open behind it

Add two Generic Camera entries, one for `/live` and one for `/live/stacked`.
Give each its own path as the Stream Source URL, and point both Still Image
URLs at `http://camera-host.lan:8099/still.jpg`. The still is what Home
Assistant shows as the thumbnail, and it is the reason a dashboard full of
camera cards does not keep the phone awake.

Worth knowing before deciding whether to add the stacked feed too: fetching
the still costs the phone nothing, since the service already keeps one ready,
while either MJPEG stream holds the phone's connection open only for as long
as something is actually watching it.

## Alerts worth having

{doc}`../supervisor/operations` lists six alerts worth wiring up — the camera
going offline, the cable socket going stale, the phone losing charge, low
battery, an overheating phone, and archive space running low — along with
what triggers each and why. Set the automations up from there once the
entities above exist; nothing here duplicates that table.

## Moving to the native integration later

MQTT discovery is meant to be a stepping stone rather than the permanent
arrangement: {doc}`integration` covers a native Home Assistant component that
talks to the JSON API directly, with no broker and no hand-added camera
entries. The move is a one-way handover of the same entity ids, so it starts
with `EXPOSE_MQTT=false` and a restart, which clears the MQTT entities and
frees their ids before the component claims them — see {doc}`integration`'s
"Moving from MQTT to the component" section for the full sequence.
