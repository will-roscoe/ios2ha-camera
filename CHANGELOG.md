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

Since 2.6.0 the JSON API under `/api/v1` is public interface too, and it carries its own version
number, reported by `/api/v1/info` as `interfaces.api`, separately from MQTT's `interfaces.mqtt`. A
change that breaks only one interface bumps only that interface's number; the package version above
still says whether anything at all has broken.

The running version is visible in three places: the first line of the container log, the
`sw_version` on the Home Assistant device page, and `./deploy.sh version`. It carries the git
description as well as the number, so an image built from a modified tree reads as `1.0.0-dirty`
rather than passing as a clean release.

## 2.11.0 — 2026-09-22

**`./deploy.sh verify` now asks Home Assistant whether the entities are actually healthy.** A
deployment can finish cleanly while the integration is unloaded or every entity is unavailable, and
until now nothing checked.

Off unless `HA_URL` and `HA_TOKEN` are both set, and read-only: it fetches `/api/states`, counts the
entities matching `HA_PREFIX` (default `ios2ha`), and names any that are unavailable. `unknown` is
not a fault -- that is a control nobody has written yet. Exit status is `0` healthy, `1` a real
problem, `2` not configured, so a script can tell "switched off" from "broken".

This is the reduced form of future-work section 5, deliberately. That proposed creating camera config
entries and installing automations, which needs an **admin** token -- a camera container holding
credentials for the whole house. The native integration has since taken the provisioning job over, so
what is left is the part needing no privilege. Home Assistant has no read-only tokens, so the
documentation says plainly: mint it from a non-admin user. The token is never logged.

No entity, topic or API change: both interface versions stay at `1`.

## 2.10.0 — 2026-09-22

**The app can now be backgrounded over the keyboard when the cable cannot do it**, as a last
resort and nothing more.

Backgrounding is what keeps this phone charged on a 3 W port, and it goes over the phone's developer
services. When those wedge, it goes with them: on 2026-09-18 the app stayed in the foreground
streaming and the battery fell from 36% to 4%. 2.9.0 recovers that by rebooting the phone, but the
reboot is budgeted and can be latched off, so there is still a window where the battery is draining
and the cable can do nothing about it.

In that window only -- the developer call has already failed, and the battery is below
`number.ios2ha_camera_emergency_below_pct` (default 15%) -- the supervisor now sends Home over the
Bluetooth keyboard it already emulates: Cmd+H, then Tab+H for Full Keyboard Access.

The keyboard is deliberately not an alternative to the cable. It is blind, where a developer call
reports what happened, so it is never the first choice and never used above the floor. What makes it
usable at all is that the claim is checked: iOS suspends a backgrounded app's listening socket, so a
refused connection is evidence the press landed, and nothing reports success without it. Foregrounding
stays a cable call, because coming back means finding the app on a Home screen this service cannot
see; a phone backgrounded this way stays dark until the cable works again, which is the trade -- a
charged phone and no camera, rather than a flat phone and no camera.

New entities, so nothing that already worked changes: `switch.ios2ha_camera_emergency_keyboard`,
`number.ios2ha_camera_emergency_below_pct`, `sensor.ios2ha_camera_emergency_background` and
`binary_sensor.ios2ha_camera_backgrounded_by_keyboard`. Both interface versions stay at `1`.

See OPERATIONS.md, "If the battery is dying and the cable cannot background the app".

## 2.9.0 — 2026-09-22

**The supervisor can now reboot the phone to clear wedged developer services**, which is the one
failure this service could see clearly and do nothing about.

Twice -- 2026-09-14 and 2026-09-18 -- the phone's DeveloperDiskImage mounter has hung and its DVT
handshake has been refused while plain lockdown carried on answering. Backgrounding goes over DVT, and
backgrounding is what keeps the phone charged on a 3 W port, so the app stayed in the foreground
streaming: the battery fell from 36% to 4%, and the second time the camera was dark for four days with
`dvt_failures` climbing to 2530 and nothing acting on it. Nothing in software recovers it -- clearing
the mount cache cannot help when the mounter is what hangs, and unplugging the cable does not clear it.
Rebooting the phone does, in about six minutes, after which the existing steps re-open the app and
streaming resumes unattended.

The diagnosis is deliberately narrow, because a reboot is the heaviest thing this service can do to the
phone: three or more consecutive developer failures, sustained for fifteen minutes, **and** a plain
lockdown call still answering. That last condition separates a wedged developer channel from an
unplugged cable, where a reboot would achieve nothing. At most two a day, six hours apart, and a reboot
that does not cure the wedge **latches rather than repeating** -- an unattended reboot loop would be
worse than the fault. The latch opens only on evidence: developer calls working again, or the new
button.

New entities, so nothing that already worked changes: `switch.ios2ha_camera_auto_reboot` (on by
default, turn it off while investigating a wedge by hand), `button.ios2ha_camera_reboot_phone` (reboots
now, ignoring the switch, the budget and the latch), `sensor.ios2ha_camera_phone_reboot` and
`sensor.ios2ha_camera_phone_reboots`. `interfaces.api` and `interfaces.mqtt` both stay at `1`.

See OPERATIONS.md, "When the phone's developer services wedge".

## 2.8.0 — 2026-09-18

**The phone screenshot is now JPEG rather than PNG**, on both the HTTP route and the retained MQTT
topic. The phone hands back a lossless full-resolution screenshot, which on a 2436x1125 screen measured
9.46 MB: no size for a retained MQTT message, and far too much for Home Assistant to fetch every time it
draws a camera thumbnail. Re-encoded at quality 90 -- chosen above the 85 the live stack uses because a
phone screen is mostly text, which is where JPEG artefacts show worst -- the same screenshot is about
260 KB, a 36-fold reduction.

The media descriptor now says `"formats": ["jpeg"]`, which is the field to read rather than assuming a
format from the path. This corrects the PNG that 2.7.0 shipped earlier the same day; both the JSON API
and MQTT still answer on the same paths and topics, so `interfaces.api` and `interfaces.mqtt` both stay
at `1` and nothing that was working stops.

A screenshot the service cannot decode is now a failed button press rather than a publish, so
undecodable bytes can no longer land on a retained topic.

## 2.7.0 — 2026-09-17

**The phone screenshot is now served over HTTP**, at `/api/v1/snapshot/screenshot`, and appears in the
media list as a fourth source (`kind: snapshot`, `formats: ["png"]`, `holds_phone: false`). It was
previously published only as a retained MQTT topic, so the `screenshot` camera entity could not be built
by anything that speaks the JSON API rather than MQTT -- which is every native Home Assistant
integration.

It serves the image the **Phone screenshot** button last captured, from memory, exactly as `/still.jpg`
serves the last stacked still: asking for it never drives the phone and never takes a new screenshot,
because taking one is the button's job. Before the first press it answers `503` rather than falling back
to a camera frame, which would answer a different question than the one asked.

Nothing is removed and no existing path moves, so `interfaces.api` stays at `1`: a client that ignores
media ids it does not recognise, as the guarantees ask, sees no change at all.

## 2.6.2 — 2026-09-17

**Stopping the Bluetooth keyboard now ends cleanly.** `stop()` stopped the keyboard's event loop
outright, which left its ten-second poll task pending -- asyncio logged `Task was destroyed but it is
pending!` on every container stop -- and left the advertisement registered with BlueZ until the bus
connection happened to drop, even though shutdown claimed to have unregistered it. It now
unregisters the advertisement first, waiting up to 1.5 s for BlueZ, then cancels the poll and stops
the loop. A slow or missing BlueZ cannot hold shutdown open: the wait is bounded and the thread is a
daemon.

## 2.6.1 — 2026-09-17

**`MQTT_HOST` now defaults to `localhost`.** It defaulted to a specific address on the developer's
own network, which is no kind of default: an install that set neither `MQTT_URL` nor `MQTT_HOST`
would have gone looking for a broker on somebody else's LAN. `localhost` is a broker on the same
machine, which is what the compose file's host networking makes it, and anything else was always
going to have to be said explicitly. An install that already sets either variable is unaffected.

Documentation: the four pages that were outlines are written — installing the service (Compose,
standalone Docker and bare Python, plus what the USB cable and the Bluetooth keyboard each buy),
connecting it over MQTT (a broker-local login and an ACL narrowing it to the two prefixes the
service uses), how releases work now that a merge to `main` publishes one, and an upgrading page
listing every version that needs the reader to do something.

## 2.6.0 — 2026-09-16

**A second way in: a versioned JSON API, with live updates, alongside MQTT.** Until now the only
integration was MQTT discovery plus hand-made Generic Camera entries. This release adds an HTTP API
under `/api/v1` that describes every entity, serves the current state, pushes changes as they
happen, accepts control writes and serves the camera's media — so that a Home Assistant custom
component, installed without any MQTT broker, can be written against it. **Nothing that works
today changes:** every MQTT entity id, topic and discovery payload is byte-for-byte what 2.5.0
published, and the existing media URLs keep working permanently.

*New: `/api/v1/info`, the one address a client needs.* It names every other route, so a route that
moves is a change to served data rather than a client release. It carries the running version,
the device block MQTT already publishes, and a separate version number per interface
(`{"api": 1, "mqtt": 1}`): the two paths can now change independently, and a change to one no
longer implies a false alarm for users of the other. Within `api: 1` responses only ever gain keys;
a client must ignore keys it does not recognise.

*New: entity descriptors, state, control and media.* `/api/v1/objects` describes all 86 entities —
id, domain, unit, device class, options, ranges, whether it can be written — from the same single
declaration MQTT discovery is now rendered from, so the two can no longer drift apart. `/api/v1/state`
serves every current value. `POST /api/v1/control/{name}` writes any switch, number, select or
button, refusing a bad value with the value actually in effect. The media is served at
`/api/v1/stream/live`, `/api/v1/stream/stacked` and `/api/v1/snapshot/still` as well as the old
paths, and each is described with whether it keeps the phone's connection open — a snapshot costs
the phone nothing, a stream holds its radio up for as long as someone watches.

*New: live updates at `/api/v1/events`.* Server-Sent Events: a full snapshot on connect, then only
the values that actually changed. A client that falls behind is disconnected rather than silently
skipped, and gets a fresh snapshot when it reconnects — a skipped update would leave it showing a
wrong value with no way to know.

*New: `EXPOSE_MQTT` and `EXPOSE_API`, both on by default.* Either interface can be switched off.
Turning MQTT off also deletes the MQTT entities from Home Assistant, which frees their entity ids
for the component to reuse; a missing broker costs one bounded attempt at startup and nothing more.
A typo in either flag keeps it on and logs a warning, rather than quietly switching an interface
off. With both off the camera still captures, archives and serves its media. **The API has no
authentication yet**: anything that can reach the port can read every value and write every
control. Keep it on a trusted network.

*New: a tiered environment-variable table* in the configuration docs — what the container needs to
run at all, what MQTT adds, what the API adds — and the entity reference page is now generated from
the code, so it cannot fall out of date.

*Fixed: a malformed control write could be believed.* A boolean control sent `off`, `False` or `no`
from a script (rather than Home Assistant's own `true`/`false`) was stored as a truthy string and
did the opposite of what was asked. Such values are now understood, and anything that cannot be
understood is refused, with the real value republished so Home Assistant does not keep showing the
rejected one. The same now holds for malformed black-level writes, which previously skipped that
republish.

*Fixed: the developer-channel failure count stayed flat during the failure it was added for.* A
wedged phone that failed while *mounting* its developer image never raised `dvt_failures`, so the
signal built after the 2026-09-14 outage was blind to that outage's own shape.

*Fixed: the noise-reduced live feed could corrupt itself.* A viewer reconnecting while a slow frame
was still being processed could leave two copies of the stacker writing to the same image at once.

*Fixed: repeated presses could stall backgrounding.* A phone-working button pressed repeatedly —
by a misfiring automation, say — queued one cable round trip per press ahead of the work that
backgrounds the app, which could quietly stop while the app kept streaming. Each such action now
runs at most once at a time; a repeat is folded into the one already in play.

*Fixed: shutdown could be killed mid-way by queued work.* Device actions still waiting when the
container was stopped were run anyway after the service had finished shutting down, which could
overrun the stop period. They are now dropped. The shutdown arithmetic has also been corrected:
the worst case is the whole 20 s grace period, not 17 s.

*Fixed: an unexpected error while connecting to the broker could stop MQTT for good*, and turning
MQTT off now also clears the retained availability message, not only the entities.

*Faster:* the capture no longer builds a large temporary array while the phone's connection is
held open (measured 200 ms–1.1 s before, 73–167 ms after, with an identical result), and night stacking no
longer repeats a full-image multiply for every frame.

## 2.5.0 — 2026-09-16

**The camera now comes back from a restart on its own.** Previously the last two steps each needed
a person: opening the app after the phone rebooted, and tapping Start streaming afterwards.

*New: the app is opened when it is not running.* A new diagnostic sensor, **App open**, reports how
that went (`idle`, `opening`, `opened`, `not_needed`, `rate_limited`, `paused`, `failed_launch`,
`unavailable`). The check runs on the battery cadence, using the answer the cable already gives for
whether the app's process exists, and it only ever acts on a definite "not running": a cold start
kills the running copy, so launching an app iOS has merely suspended would throw away a stream that
would have woken by itself, and a cable that cannot answer means *unknown*, not dead. At most 3
starts an hour and 12 a day, ten minutes apart, and a success does not refund that budget — an app
that dies the moment it opens must not be relaunched all night.

*Fixed: two threads could open a developer session at the same time, and wedge the phone.* iOS
serves one instruments session at a time. The health tick asking whether the app is running, and
the step that opens the app asking the same thing, each opened their own — and when they overlapped
both hung until their timeouts and the phone's side stayed wedged, so every later call failed too.
Only restarting the container, which drops the sessions, recovered it. This is what actually kept
the camera dark for two and a half hours on the night of 2026-09-16, with a healthy phone on the
end of a healthy cable: lockdown answered in milliseconds throughout, and an instruments call made
with the container stopped returned 344 processes in one second. Developer-channel calls are now
serialised inside `PhoneManager`, so they queue rather than collide.

*The app is asked for on the health cadence, not only when something changes.* A phone that has just
restarted cannot answer over the cable for a minute or two while its developer image remounts, so
the attempt triggered by the state change can only report that it cannot tell — and a stream state
that then sits at `backgrounded` never changes again, so nothing asked a second time. Measured the
hard way: the camera stayed dark for two and a half hours with a perfectly healthy phone on the end
of the cable. The 30-second health tick now retries while nothing is answering, with a flag so
attempts cannot pile up behind one that is blocked on the mount's own two-minute timeout.

*Fixed: "is the app running" could not answer honestly.* The process was matched on the last word
of its bundle id, which for `com.guardianeye.webcam.ios` is `ios` — and that matches `apfs_iosd`,
present on every iPhone. The app therefore read as running on a phone where it had been deleted,
and every part of the recovery that asks the question first was quietly built on it. The executable
name now comes from the phone's own app list, looked up once by bundle id and remembered, so it is
exact and stays right when the phone or the app is swapped.

*Also new: a running app that answers nothing is brought to the front.* After a phone restart iOS
relaunches Guardian Eye by itself, backgrounded and serving nothing — a process exists, so a cold
start would be both wrong and destructive. That case is fronted instead, never while the battery
policy has deliberately backgrounded the app, since that pause is meant to be held.

The three fixes below make the keyboard half of that reliable.

*The keyboard survives a container restart, and can recover an app that has stopped listening.*

*Pinned GATT handles.* BlueZ gave the HID service fresh handles on every registration — 0x00b8, then
0x00cc, then 0x00e0 — and iOS caches the handles of a bonded keyboard: on reconnect it re-discovers
the device but never re-reads its report map. So a pairing made against one container worked, and
then silently did nothing after the next restart, with the phone still listed as connected and the
host still transmitting. The service is now registered at a fixed handle (0x0200, its report
characteristic at 0x0208), which BlueZ honours, so a restart no longer breaks the pairing.

*More than one key at a time.* A report can now carry several key usages, because iOS Full Keyboard
Access chords on Tab rather than on a modifier: Home is Tab+H, and Tab+Z lists its commands. The
letters are available for iOS's own shortcuts, along with minus and power.

*A lock-and-return repair.* A cold-started app sometimes comes up accepting no keyboard input at
all — keys on the wire, nothing on screen, and none of Full Keyboard Access's blue border. Locking
the phone with Ctrl+Cmd+Q and coming back to the app restores it, and unlike relaunching the app it
is reversible: the same screen returns. When its first press has no effect, the start step now locks
the phone, wakes it, brings the app back over the cable, and tries once more. It does this at most
once per attempt, only when it has a way back into the app, and only if the app is confirmed back
and still idle — and if the stream turns out to be up afterwards it says so rather than reporting a
failure, since the supervisor's answer to a failure is another attempt, and a second run of the
sequence on a streaming app lands on *Stop* streaming. The wake key is Escape, not Space: if the
lock chord ever misses, Space would activate whatever the focus frame is sitting on.

## 2.4.1 — 2026-09-16

**The keys never left the host.** 2.4.0 sent its key presses from the supervisor's device worker,
but the Bluetooth stack it uses is asyncio: a notification emitted from a foreign thread never
reaches the bus. Every attempt looked perfect — the phone stayed subscribed, no error was raised,
`press` reported success — and nothing moved on the phone. Verified on the device: three attempts,
and the screen was byte-identical throughout.

Key sequences now run on the keyboard's own event loop, with the caller waiting for the result. The
spike this was ported from did the same thing, which is why it worked; the port lost it.

## 2.4.0 — 2026-09-16

**The supervisor can now start the stream itself, so the camera comes back after a power cut with
nobody touching the phone.** Guardian Eye starts streaming from a button in the app and nothing
else: no URL, no shortcut, and the accessibility service's own press is refused for App Store apps.
What does work is a real keyboard, because iOS Full Keyboard Access turns key presses into focus
moves and activations. So arcturus becomes one.

- **A Bluetooth keyboard, emulated by the host.** HID over GATT through BlueZ, reached only through
  a D-Bus proxy filtered to `org.bluez`, installed by `scripts/install-bluez-proxy.sh`. The host's
  own Bluetooth keyboard and mouse are untouched. Nothing is tied to one phone: whatever pairs is
  the phone, so a replacement needs no configuration.
- **A narrow, guarded step.** Keys are sent only when the app's process is running *and* its server
  answers 503, which means open and idle. That answer is re-read immediately before Space, because
  the sequence takes about fifteen seconds and Space on a streaming app is *Stop* streaming. A
  refusal (backgrounded, locked, not in front) is left to the flows that already handle it.
- **Bounded attempts:** six an hour, twenty a day, two minutes apart, widening after failures. A
  phone that keys cannot fix is not poked all night.
- **`switch.ios2ha_camera_auto_start`** pauses the whole thing, so a stream you stop on the phone
  stays stopped. **`button.ios2ha_camera_start_streaming`** presses now, ignoring the switch and the
  limit, but never the guards.
- **New readings:** `sensor.ios2ha_camera_stream_start` (how the last attempt went),
  `sensor.ios2ha_camera_stream_start_attempts`, `binary_sensor.ios2ha_camera_keyboard_connected`,
  and `sensor.ios2ha_camera_dvt_failures`.

**Fixed: a wedged developer connection stayed wedged.** On 14 September one `ConnectionTerminatedError`
left every call needing the phone's developer services failing for 24 hours. Backgrounding stopped,
the app kept streaming, and the battery ran flat. The developer disk image was mounted once and the
result cached for the life of the process, so nothing re-checked it. A failure now clears that cache
and is counted, and the count is published.

**One-time phone setup** for the keyboard (see OPERATIONS.md): no passcode, Bluetooth on, Full
Keyboard Access on, and pair the keyboard once. It appears in the phone's Bluetooth list under the
host's own name, because iOS shows that rather than the advertised one.

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
