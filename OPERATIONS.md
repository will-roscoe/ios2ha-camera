# Operations

Running the supervisor, and the things about this particular installation that will otherwise cost
you an evening.

## Deploying

The container builds from a copy of this repository on the Docker host — the host cannot see the
development machine's filesystem, so the build context has to live beside the compose file.

**Always deploy with `deploy.sh`**, not by hand:

```bash
./deploy.sh            # sync, build, restart, and show the version line
./deploy.sh version    # what is running right now
./deploy.sh check      # is the deployed tree identical to this one?
```

It exists because the deployment is an rsync of a working tree, so nothing else records what is
actually running. The script bakes `git describe --always --dirty --tags` into the image, and warns
before deploying a modified tree. Deploy by hand and the running version reads `unknown`.

It needs to know where to deploy: `IOS2HA_HOST` (the ssh destination of the Docker host),
`IOS2HA_SRC` (the build context on the host, which the tree is synced to) and `IOS2HA_STACK` (the
directory holding the compose file). Set them in the environment, or copy `deploy.env.example` to
`deploy.env` beside the script (untracked) and fill it in; a variable already in the environment
wins over the file. Without them the script stops before touching the host and names what is missing.

A source-only rebuild takes about 4 seconds: dependencies are installed from `pyproject.toml`
against a stub package first, so the ~100-package layer is cached and a code change does not
reinstall opencv and numpy. The provenance build args come last in the Dockerfile for the same
reason.

### Checking Home Assistant after a deploy

`./deploy.sh verify` asks Home Assistant whether this service's entities are actually
there and healthy, which the deploy itself cannot know: the container can be perfectly
up while the integration is unloaded or every entity is unavailable.

It is **off unless `HA_URL` and `HA_TOKEN` are set** in the container's environment, and
it only ever reads. Home Assistant has no read-only tokens, so mint the token from a
**non-admin user** — nothing here writes, and that way nothing here *can*.

```
./deploy.sh verify
94 entities, none unavailable
```

Exit status is the useful part: `0` healthy, `1` a real problem (unreachable, no entities
found, or some unavailable, each named), `2` not configured — which the script reports as
a note rather than a failure, because switching the check off is not a broken deployment.

### Preparing the phone after a restart

```bash
./deploy.sh phone                     # works even while the container is stopped
./deploy.sh phone --fix-developer-mode  # also turns Developer Mode on. REBOOTS THE PHONE
```

This checks lockdown is reachable, whether Developer Mode is on, mounts the developer disk image,
relaunches the camera app if it isn't running, and reads the battery — then waits up to five minutes
for someone to open Guardian Eye and tap Start streaming, confirming with the same probe the
supervisor itself uses that frames are actually arriving. Streaming cannot be started any other way:
there is no ONVIF call or API for it.

If the supervisor's container is already running, `deploy.sh phone` skips that wait automatically —
the running service is already probing the app on its own schedule, and a second probe loop knocking
on it every 5 s at the same time is exactly the connection churn that wedges it. It still does the
setup steps and prints a reminder to watch `sensor.ios2ha_camera_stream_state` in Home Assistant
instead.

`--fix-developer-mode` is the only way this turns Developer Mode on, and only on request: doing so
reboots the phone. The `button.ios2ha_camera_prepare_phone` entity runs the same setup steps over
MQTT without a shell, but never that flag and never the wait — it reports what it found as an event
and returns immediately.

## Versioning

See [CHANGELOG.md](CHANGELOG.md) for what the numbers mean. In short, the Home Assistant entity and
topic surface is the public interface: removing or renaming an entity id is a **major** change,
adding entities is **minor**, anything else is a **patch**.

To release:

1. Bump `version` in `pyproject.toml` — the only copy; the package reads it back at runtime.
2. Add the entry to `CHANGELOG.md`.
3. Commit, then `git tag -a vX.Y.Z -m "..."`.
4. `./deploy.sh` from the tagged commit, so the image reads `X.Y.Z (vX.Y.Z)` rather than a commit
   distance.

The running version shows in three places: the **first line of the container log**, the
**`sw_version` on the device page** in Home Assistant, and `./deploy.sh version`. A build from a
modified tree reads `-dirty`, deliberately.

### The compose service

Host networking, because the service must reach the phone, the broker and the USB socket together.
It mounts `/var/run/usbmuxd` and `/var/lib/lockdown` (read-only) for battery access, and a data
directory for the archive.

`stop_grace_period: 30s` matters. Worst case is 20 seconds, leaving 10 s of margin: up to 4 s for the
event loop to finish or cancel a `/live` viewer's pending write, 5 s for the upstream connection to
close, 3 s to stop the Bluetooth keyboard, two 3 s thread joins, and up to 2 s to publish "offline"
over MQTT — see [ARCHITECTURE.md](ARCHITECTURE.md) for the full arithmetic. A device action already
running on the cable when the stop arrives is waited for on top of that; one stuck on the cable can
outlast the period. Measured end to end with a viewer attached: 1.1–5.1 s in practice. The margin
is there because a longer `stop_grace_period` costs a normal stop nothing; do not set it below 20 s,
which would leave the worst case no room at all. Before the on-demand connection, the same shutdown could take 122 s with a viewer
attached — the ingest thread waiting out a connect to an unresponsive phone before the offline
availability was ever published — which is longer than even this grace period, so the container was
SIGKILLed mid-shutdown and Home Assistant kept showing the camera as online after a planned restart.

### The MQTT account

Use a **broker-local login**, and do not create a Home Assistant account for this. The Mosquitto add-on
takes a list of username and password pairs in its `logins` option; those accounts exist only in the
broker. Any other broker works the same way through its own `passwd` file.

The add-on will also authenticate Home Assistant users, and an earlier version of this document
recommended that. It was the wrong advice: a Home Assistant login can reach far more than MQTT, and
needing one to install a Home Assistant app is not a reasonable thing to ask of anyone.

Two things worth knowing about add-on logins. Their passwords are stored as plain text in the add-on's
options and can be read by anything with Supervisor API access, so give this one its own password and
nothing else. And they can be narrowed further: set `customize.active` and supply an ACL from
`/share/mosquitto` that limits the account to `ios2ha_camera/#` and the `homeassistant/#` discovery
prefix, so a leak of it cannot touch the rest of your broker.

## Rolling back

Two different things can be meant by "roll back", and they fix different problems:

- **Turn `switch.ios2ha_camera_on_demand` off.** This is instant, needs no deploy, and restores the
  previous always-connected behaviour — the phone connection is held open for the life of the
  process again. Reach for this if the on-demand connect/disconnect cycle itself looks like the
  problem (see "The idle probe" under Health and alerts, below).
- **Redeploy an older tag** with `./deploy.sh` from that tag's commit, if the fault is in the code
  itself rather than in the connection policy.

**A downgrade from 2.0.0 needs your automations edited back**, not just the container rolled
back. 2.0.0 renamed every `stream_state` value: `unreachable` split into `unavailable` and
`backgrounded`, `idle` became `ready`, and `streaming` split into `capturing`, `in_use` and
`streaming`. Anything matching those by name — the offline alert's template especially — was
edited to the new names when you upgraded and will silently stop matching on the way back.
The retained-value problem below applies too, in both directions.

A downgrade to a version before 1.1.0 needs one extra step. `stream_state` gained a new option,
`idle`, in 1.1.0; an older version's Home Assistant discovery payload does not list it, so a
retained `idle` value left over from the newer version is not one of the older enum's valid options
and Home Assistant will show the sensor as unavailable until it changes. The older version's health
tick republishes its own real state within 30 seconds regardless, which clears this on its own — but
to fix it immediately, clear or republish the retained `ios2ha_camera/state/stream_state` topic by
hand after the downgrade.

The same applies to `stalled`, added in 2.3.0: a version before it does not list the option, and a
retained `stalled` makes the sensor unavailable until the older version's first health tick.

## Reading the logs

A healthy run is quiet. One line per still, one per state change, nothing else:

```
supervisor up: phone 192.168.1.11:8080, live view on :8099, interval 600s, archive /data/archive
ingest stream state None -> streaming
supervisor still: depth 6 of 6, subject 39.6, 5.9 s, archived /data/archive/2026/09/12/185238.jpg
```

That is deliberate. Anything routine that appeared in the log would make a real problem harder to
notice, so ordinary conditions — a viewer disconnecting, the broker being unreachable at shutdown,
the developer image already being mounted — are all logged at INFO or not at all, without
tracebacks. A traceback means something genuinely unexpected.

`depth N of M` shows the depth achieved against the depth asked for. They diverge when the buffer
did not hold enough frames inside the capture window, and the MQTT event carries `short: true`.

A still that could not be made is a warning, `still failed: <reason>` — most often
`no frames: <stream state>`. Before 2.13.0 that reason went only into the MQTT event, so with MQTT
off a camera that had stopped producing stills left no trace in the log at all.

## The control page

`http://<host>:8099/` (it redirects to `/ui/`) shows every entity Home Assistant has -- readings,
switches, numbers, selects and buttons -- with what each one does, updating live, and the picture.
It is a client of the JSON API like the Home Assistant component: it hardcodes only
`/api/v1/info`, writes through `/api/v1/control/{name}`, and shows the API's own error when a
write is refused. So it exists exactly when `EXPOSE_API` is on, and needs no Home Assistant at all
-- useful when Home Assistant is the thing that is down.

The Stacked and Live views keep the phone connected, and awake, for as long as they are shown: they
start only when you press Start, and stop when you switch view or the browser tab is hidden. Like
the rest of the API the page has no login and is meant for the LAN only; do not publish port 8099.

## Health and alerts

Availability is a last-will topic plus frame age. With the connection on demand, `camera_online` is
true while active with a frame within 30 seconds, **or** while `ready` with a frame within 150
seconds — idle is the normal state whenever nothing is capturing or watching, so it must not read as
offline on its own. It is forced **false** whenever the upstream thread's own heartbeat
(`sensor.ios2ha_camera_tick_age`) has gone more than 180 seconds without ticking, regardless of what
state it last reported — a wedged or dead thread must not leave every reading frozen at whatever it
last saw as healthy.

**The idle probe.** While nothing holds the connection, it is probed every 60 seconds — connect, read
one frame, close, for about 2–3 mA a minute — which is what keeps `ready` fed with a recent frame and
lets a genuine outage still be noticed between captures. A failing probe backs off (60, 120, 240 s,
capped at 300 s) rather than knocking on a wedged app every minute. If the app itself starts
wedging or needs relaunching more often than it used to, the probe's own connect/disconnect cycle is
worth suspecting — turn `switch.ios2ha_camera_on_demand` off to fall back to one steady connection
and see whether the problem goes away.

**Expected duty cycle.** A night capture takes a hold for roughly 60–75 seconds of every 600 second
interval — the time it takes to gather its frames, not the render, which happens after the hold is
released. `sensor.ios2ha_camera_duty_1h` should sit around 10–12.5% with nothing else attached. A
much higher reading for any length of time — with `sensor.ios2ha_camera_holders` showing `viewer` or
`viewer (linger)` rather than settling back to `none` — means something is holding the phone
connected well beyond what capturing needs.

**A slow viewer.** A `/live` or `/live/stacked` client whose connection can't keep up with the JPEG
stream — under roughly 1.2 Mbit/s, measured on loopback — is disconnected after 30 seconds of a
stalled write, the same as one that genuinely went away. This exists so a stalled viewer cannot pin
the phone connection open indefinitely; a shorter timeout was tried first and wrongly dropped real
viewers on slow links.

Six alerts are worth having:

| Alert | Trigger | Why |
|---|---|---|
| Offline | `camera_online` off or unavailable for 5 min | The original failure; unavailable means the supervisor itself stopped reporting. See below — the message should name what is actually wrong |
| Cable down | `cable_state` is `usbmuxd_down` for 10 min | The socket mount is orphaned: battery, app control and **Prepare phone** are all dead, while the WiFi stream carries on as if nothing were wrong. Do **not** alert on `no_device`, which is just the cable being out |
| Losing charge | Battery current negative for 20 min | The supply cannot keep up; invisible in "charging". Check `sensor.ios2ha_camera_holders` and `sensor.ios2ha_camera_duty_1h` first — a connection stuck open (a viewer that never left, or `on_demand` switched off) looks exactly like a weak charger on the battery current alone |
| Battery low | Below 40% | It will stop capturing when the phone dies |
| Battery hot | Above 42 °C for 10 min | Charges and streams continuously |
| Archive space low | Below 5 GB | Archiving stops at 2 GB |

A caveat learned the hard way: Home Assistant will report a notification as delivered whether or not
your phone ever receives it. If the companion app has lost its connection, `notify` still succeeds.
Test the whole path occasionally rather than trusting that an alert exists.

### Making the offline alert say what is wrong

Before 2.0.0 the offline alert could only say "the camera is offline", because
`stream_state` had one word for four unrelated situations. It now distinguishes them, and
the message should too — the fix is different in every case, and three of the four do not
need anyone to get up.

```jinja
{% set s = states('sensor.ios2ha_camera_stream_state') %}
{% if trigger.to_state.state == 'unavailable' %}
  No word from the supervisor for 5 minutes: the ios2ha-camera container on
  the host is stopped, or MQTT is down.
{% elif states('sensor.ios2ha_camera_tick_age') | float(0) > 180 %}
  The supervisor is running but its upstream thread has stopped ticking, so
  every reading is stale. Restart the container.
{% elif states('sensor.ios2ha_camera_phone_reboot') in ['still_wedged', 'given_up'] %}
  The phone's developer services are wedged and a reboot did not clear them, so
  no further reboot will be attempted. See "When the phone's developer services
  wedge" -- this is the one case that needs a person.
{% elif is_state('binary_sensor.ios2ha_camera_backgrounded_by_keyboard', 'on') %}
  The app was backgrounded by keyboard to save the battery, and it cannot be
  brought back until the cable works again. The phone is charging, the camera is
  dark: clear the wedge and it resumes.
{% elif s == 'streaming_off' %}
  The app is running but not streaming. Open Guardian Eye and tap Start streaming --
  nothing else can do this.
{% elif s == 'streaming_off' and
        states('sensor.ios2ha_camera_stream_start') == 'failed_keyboard_not_connected' %}
  The app is idle and the supervisor could not press Start streaming: no phone is
  attached to its keyboard. Check Bluetooth is on on the phone.
{% elif s == 'stalled' %}
  The app's picture has frozen: it still answers but sends nothing. Open Guardian
  Eye, stop streaming and start it again. If that does not help, press Relaunch app.
{% elif s == 'backgrounded' %}
  {% if is_state('binary_sensor.ios2ha_camera_app_running', 'off') %}
  iOS has killed the app. Press Relaunch app, then tap Start streaming.
  {% elif is_state('binary_sensor.ios2ha_camera_backgrounding_active', 'on') %}
  Backgrounded on purpose to save battery. Nothing to do -- it wakes for the
  next capture. (This branch should not normally alert: camera_online stays on
  for a deliberate backgrounding.)
  {% else %}
  The app is backgrounded, so its socket is suspended. Foreground it to resume.
  {% endif %}
{% elif s == 'unavailable' %}
  The phone is not answering at all. Check WiFi and that it is powered.
{% elif is_state('binary_sensor.ios2ha_camera_reachable_over_cable', 'off') %}
  The phone is streaming, but the cable is down: no battery readings and no app
  control. See "When the cable stops working".
{% else %}
  Camera offline, state {{ s }}.
{% endif %}
```

The first two branches must come before any `stream_state` test, and both are in the
live automation: a supervisor that has stopped reporting leaves `stream_state` frozen at
whatever it last said, so reading it would describe the phone when the fault is the
container. `tick_age` above 180 s is the same trap one level down -- the container is up,
its upstream thread is not, and every other reading is stale.

The two branches after them are the failure modes 2.9.0 and 2.10.0 introduced. Neither is
a camera fault: `phone_reboot` latched at `still_wedged` means the automatic recovery has
run out of road, and `backgrounded_by_keyboard` means the battery was saved at the cost of
the picture. Both need a person, and neither is obvious from `stream_state` alone.

The `app_running` branch is the one worth having. A backgrounded app and one iOS has killed
look identical over WiFi — both refuse the connection — and only the cable can tell them
apart. One resumes by itself when foregrounded; the other needs relaunching. Note that
`app_running` is **unknown**, not `off`, when the cable cannot answer, so
`is_state(..., 'off')` correctly declines to claim the app is dead on no evidence.

### Starting the stream without touching the phone

Guardian Eye starts streaming from a button in the app and nothing else. After the phone restarts —
a power cut, a flat battery — it cold-starts with streaming off, and until 2.4.0 only a person
tapping that button could fix it. The supervisor now presses it, by being a Bluetooth keyboard: iOS
**Full Keyboard Access** turns real key presses into focus moves and activations, and the sequence
is Tab, Down forty times, Space. Start streaming is the last item on the page, so enough Downs
always land on it.

**Getting into the app is separate and happens over the cable.** The keyboard is used only for that
one button, and only when the app is already open and idle.

**It presses keys only when all of this holds**, each re-checked at the moment it matters:

- the app's process is running, over the cable, and
- the app answers **503**, which means open with streaming off. A refused connection means
  backgrounded, locked or not in front, and is left to the flows that handle those, and
- `switch.ios2ha_camera_auto_start` is on, and
- the attempt budget allows it.

The 503 check is repeated immediately before Space, because the Downs take about fifteen seconds. On
a streaming app that last item reads **Stop streaming**, and on the lock screen the keyboard's focus
sits on the torch, so a press at the wrong moment would do real damage.

**Attempts are budgeted:** six an hour, twenty a day, at least two minutes apart. When the budget is
spent, `sensor.ios2ha_camera_stream_start` reads `rate_limited` and the supervisor stops trying — a
phone that keys cannot fix is not worth poking all night.

| `stream_start` | What it means |
|---|---|
| `idle` | Nothing attempted since startup |
| `starting` | Keys are going out now |
| `started` | The app is streaming again |
| `not_ready` | The app is not running, or not answering on its page. Nothing was pressed |
| `already_streaming` | It was already up, so no keys were sent |
| `paused` | The auto-start switch is off |
| `rate_limited` | Budget spent; a person is needed |
| `failed_keyboard_not_connected` | No phone is attached to the keyboard — usually Bluetooth off on the phone |
| `failed_keys_had_no_effect` | Keys were sent and the app still is not streaming |

**`button.ios2ha_camera_start_streaming`** presses now, ignoring both the switch and the budget,
because a person asking is an explicit instruction. It still obeys every guard above.

### Before the keys: opening the app

Pressing Start streaming needs an app that is open and idle, so a step before it makes the app
present at all. After a phone restart iOS relaunches Guardian Eye by itself, backgrounded and
serving nothing, so the usual repair is to bring it to the front rather than to cold start it — a
cold start kills the running copy, and with it any stream it was holding.

That step runs when nothing is answering on the app's page, on the 30-second health tick as well as
on a state change, because a phone that has just booted cannot answer over the cable for a minute or
two and its state then stops changing. It is budgeted at three starts an hour and twelve a day, ten
minutes apart, and a success does not refund the budget: an app that dies as soon as it opens must
not be relaunched all night.

| `app_open` | What it means |
|---|---|
| `idle` | Nothing attempted since startup |
| `opening` | A launch or a foreground is going out now |
| `opened` | The app was not running and was cold started |
| `fronted` | The app was running but answering nothing, so it was brought to the front |
| `not_needed` | The app is already running, and there is no way to front it |
| `paused` | The auto-start switch is off, or the battery policy has deliberately backgrounded the app |
| `rate_limited` | Budget spent; a person is needed |
| `failed_launch` | The launch or foreground was refused, or the app still is not running afterwards |
| `unavailable` | The cable could not say whether the app is running. Not the same as "it is not" — nothing is launched on a guess, because that would kill a stream that was merely suspended |

**One-time setup on the phone**, all of it needed before any of this works:

1. **No passcode** (Settings → Face ID & Passcode → Turn Passcode Off). After a restart a passcode
   blocks USB entirely, so nothing can even open the app.
2. **Bluetooth on.** It cannot be switched on over the cable, so if it is off the supervisor can only
   report `failed_keyboard_not_connected`.
3. **Full Keyboard Access on** (Settings → Accessibility → Keyboards).
4. **Pair the keyboard once**, from Settings → Bluetooth. It appears under the *host's* Bluetooth
   name (the host's own name), not the advertised name: iOS shows the adapter's own name.

A replacement phone needs exactly these four steps and nothing else; no address or name is
configured anywhere.

**On the host**, `scripts/install-bluez-proxy.sh` installs `bluez-dbus-proxy.service`, which gives
the container a D-Bus socket that reaches BlueZ and nothing else. Without it the keyboard reports
`unavailable` and everything else carries on unchanged. `./deploy.sh check` says whether it is there.

### Backgrounding to charge the phone

Streaming costs roughly 300 mA and this installation runs from a 3 W port, so a phone that
streams continuously never recovers. Backgrounding the camera app pauses the stream without
stopping it — iOS suspends the listening socket and leaves the process running — and that is
worth around 370 mA. Measured on the device: **30% to 96% in under three hours**.

Below `background_below_pct` (50% by default) the supervisor backgrounds the app whenever
nothing needs frames, and foregrounds it again the moment something does. Above
`resume_above_pct` (80%) it stops. In between it carries on with whatever it was doing, which
is what stops a phone resting near the threshold flipping back and forth.

While this is happening:

- `binary_sensor.ios2ha_camera_backgrounding_active` is **on**.
- `sensor.ios2ha_camera_stream_state` reads `backgrounded` between captures.
- **`camera_online` stays on**, so the offline alert does not fire. That exemption applies
  only to a backgrounding this service performed; a phone backgrounded by anything else still
  alerts, and so does a real outage while the policy is engaged.
- Captures take a few seconds longer, while the app comes back to the foreground.

Turning `switch.ios2ha_camera_backgrounding` off foregrounds the app immediately rather than
waiting for the battery to recover.

One thing to expect on the phone itself: backgrounding works by foregrounding **Settings**,
because there is no "go home" over usbmux and killing the app would need someone to tap Start
streaming afterwards. So the phone's screen shows Settings rather than the camera preview
between captures. Harmless with Screen Curtain on, but it does mean you cannot tell at a
glance whether the camera app is alive — `binary_sensor.ios2ha_camera_app_running` answers
that instead.

### When the phone's developer services wedge

Twice now, on 2026-09-14 and 2026-09-18, the phone has stopped answering **developer** calls while
answering everything else. The signature is specific:

- `sensor.ios2ha_camera_dvt_failures` climbs and stays up (2530 by the end of the second outage).
- The disk image mounter hangs, and the DVT handshake is refused in a tenth of a second.
- Plain lockdown services keep working, so battery readings and screenshots still arrive.

It matters because **backgrounding goes over DVT**, and backgrounding is what keeps the phone
charged on a 3 W port. The second time, the app stayed in the foreground streaming, the battery
fell from 36% to 4%, and the camera was dark for four days because nothing acted on it.

**It does not recover on its own.** Clearing the mount cache cannot help when the mounter is what
hangs, and unplugging the cable does not clear it either. Rebooting the phone does, in about six
minutes, after which the app is re-opened and streaming resumes unattended.

So the supervisor now reboots the phone itself, on a deliberately narrow diagnosis: three or more
consecutive developer failures, sustained for fifteen minutes, **and** plain lockdown still
answering — that last one is what distinguishes a wedged developer channel from an unplugged cable,
where a reboot would do nothing. **At most three a day, at least twenty minutes apart**, kept in
`REBOOTS_FILE` so the budget holds across restarts.

That last part matters more than it looks: **every phone reboot restarts this service** about a
minute later, because the phone dropping off USB restarts usbmuxd and the usbmuxd hook restarts the
container. Until 2.14.0 each restart forgot the budget, and the week to 2026-09-28 saw nine reboots,
two of them 22 minutes apart on the 27th — the second being the one that cured the wedge, which is
why the gap is now twenty minutes rather than the six hours first set. The same restart also used to
cut the post-reboot check short: the dying process logged "still wedged, giving up" on the 26th and
27th while the phone was in fact fine. Now a reboot whose settle is interrupted by the service
stopping gives no verdict, and the next start watches the developer channel instead.

If a reboot does not clear it, the service **gives up rather than trying again**: the state latches
at `given_up`, and only the developer channel working again — or the `button.ios2ha_camera_reboot_phone`
button — releases it. A reboot loop overnight would be worse than the wedge.

| `sensor.ios2ha_camera_phone_reboot` | What it means |
|---|---|
| `not_needed` | The developer channel is healthy, or has not been failing long enough |
| `rebooting` | The restart has been requested; the phone takes a couple of minutes |
| `recovered` | Developer calls work again |
| `still_wedged` | The reboot did not fix it. **Look at it yourself**; nothing further will be attempted |
| `given_up` | That latch is set |
| `unavailable` | The phone does not answer at all, so this is a cable fault, not a wedge — see below |

Switch `switch.ios2ha_camera_auto_reboot` off while you are investigating a wedge by hand, or the
service may reboot the phone underneath you. The button ignores the switch, the budget and the latch.

### If the battery is dying and the cable cannot background the app

Backgrounding normally goes over the cable, and that is the route to keep: it
reports what actually happened. When the developer channel is wedged it is gone, and
on a 3 W port a foreground stream drains the phone within hours.

So, **only** once `phone.background()` has failed and the battery is below
`number.ios2ha_camera_emergency_below_pct` (default 15%), the supervisor sends Home
over the Bluetooth keyboard instead: Cmd+H first, then Tab+H for Full Keyboard Access.

It is blind, which is why it is last. What makes it usable is the check afterwards: iOS
suspends a backgrounded app's listening socket, so the app refusing a connection is
real evidence the press landed. `sensor.ios2ha_camera_emergency_background` reads
`backgrounded` only on that evidence, and `failed_keys_had_no_effect` when the app
keeps answering.

**It cannot put the app back.** Foregrounding stays a cable call, because coming back
means finding the app on a Home screen this service cannot see. So a phone backgrounded
this way stays dark until the cable works again -- which is the trade being made: a
charged phone and no camera, rather than a flat phone and no camera.
`binary_sensor.ios2ha_camera_backgrounded_by_keyboard` says when you are in that state,
and the fix is the reboot above.

Switch `switch.ios2ha_camera_emergency_keyboard` off to forbid this entirely.

### When the cable stops working

The container reaches the phone two ways: over WiFi for frames, and over the Lightning cable via
`usbmuxd` for battery readings and app control. Compose bind-mounts usbmuxd's socket as a **file**:

```yaml
- /var/run/usbmuxd:/var/run/usbmuxd
```

`usbmuxd` runs with `--systemd`, which implies `--enable-exit`: it exits whenever no device is
attached, and creates a **new socket, with a new inode**, when udev starts it again. The container is
still holding the old, deleted inode, so from then on every cable operation fails with
`ConnectionFailedToUsbmuxdError` — permanently, until the container is recreated. Nothing about the
WiFi stream changes, so stills keep being produced and the fault is invisible unless you are watching
the battery figures. On 13 September this cost two hours.

There are two defences, and the first is much better than the second.

**On the host, run this once.** It is the real fix and it acts within seconds:

```bash
sudo ./scripts/install-usbmuxd-hook.sh
```

It adds an `ExecStartPost` drop-in to `usbmuxd.service` that restarts the container as soon as
usbmuxd comes back. It is guarded so it cannot make things worse: it runs via `systemd-run
--no-block`, so usbmuxd's own startup is never delayed by a container restart; it uses
`ExecStartPost=-`, so a failure can never stop usbmuxd itself; it only restarts a container that is
already running, so one you stopped deliberately stays stopped; it exits quietly when Docker is not
up yet, which is normal during boot; and it rate-limits to one restart a minute, because udev starts
usbmuxd on **every** attach and a cable with a bad contact would otherwise restart the container
continuously. `sudo ./scripts/install-usbmuxd-hook.sh --uninstall` reverses it, and re-running it is
harmless.

Check it is working with `journalctl -t ios2ha-camera-reattach`.

**In the container, as a fallback** for hosts without that hook, the supervisor restarts itself once
usbmuxd has been unreachable for five minutes, and Docker's `unless-stopped` policy brings it back on
a fresh socket. It is deliberately slow, and it will not fire in the first ten minutes of a process
(a cable already broken at boot would otherwise loop forever), nor twice within an hour, nor for a
phone that is merely unplugged. Recognise it in the log by:

```
restarting: usbmuxd has been unreachable long enough that the socket mount is presumed orphaned.
```

Seeing that line means the host hook is not installed, or did not work.

**The manual fix**, if you are looking at a broken cable right now:

```bash
docker compose up -d --force-recreate ios2ha-camera
```

A plain `docker restart` is enough too — what matters is that the bind mount is re-resolved.

## Keeping the picture in focus

The phone's own autofocus is not good enough at night, and a fixed lens position is not either.
Measured 2026-09-28 on a skyline through a window: night autofocus parked the lens at 0.68 while
the sharpest position was 0.78-0.82, with about five times the edge detail; by 23:00 the best had
drifted to 0.77. So the service focuses itself.

**A focus run** (`button.ios2ha_camera_focus_now`, and once each dusk with
`switch.ios2ha_camera_focus_at_dusk`) searches lens positions 0.60 to 1.00 only. Anything within
about a metre -- the window, a curtain -- focuses below 0.60, so it is never considered; 1.00 is the
far end of the lens's travel, past infinity. At each position it lets the lens settle and averages
~3 s of frames, because a single night frame is too dark to judge, and scores the edge detail of the
most detailed quarter of the picture, border excluded. It probes 1.00, 0.90, 0.80, 0.70 and 0.60,
brackets the farthest peak within 10% of the best -- near-equal peaks go far -- and narrows it by
golden-section comparison to 0.01. About 40 s, and the whole score curve is logged.

A result is kept only if it beats the position it would replace, measured then and there: the held
position is scored either side of the result, which cancels a steady change in the light, because
contrast changing through a run -- lights coming on at dusk, the phone changing exposure -- can fake
a peak. A kept result is written to `focus_position`, held exactly as a value you type would be.
Otherwise nothing changes: no clear peak (fog, darkness, a covered lens), not better than the held
focus, or a phone that failed the run -- which also puts the lens back.
`sensor.ios2ha_camera_focus_run` says which.

A run first waits for the phone to answer: at dusk the battery policy has usually backgrounded the
app, which refuses every call until the session the run opens has woken it.

**The dusk run** follows a still taken in the afternoon or evening whose scene needs more brightening
than the one before -- the light is falling -- but still has light for edges (night weight between 0
and 0.5). Dawn does not count, although the light passes through the same band. It runs once a day
on success, tries at most three times, and only while autofocus is off -- with autofocus on the
phone is in charge.

**The lens is re-driven at every capture.** The phone reports a lens position, but it is not to be
trusted: after the app's capture session restarted on 2026-09-28 it reported 0.804 while the lens
sat elsewhere, and stills went soft. So every session moves the lens 0.03 away and back to the held
position rather than believing it is already there.

## Holding the night colour

The phone's automatic white balance moves at night and cannot come back without light. Measured
2026-09-28: covering the lens left stills cyan (green/red 1.48 against 0.97) for the rest of the
night; a curtain opening behind the camera made them bluer everywhere; restarting the app's capture
session fixed it once and made it worse the second time. The app exposes no white-balance control.

So the service holds the colour itself:

1. On a night still whose colour looks right, choose a region of the view that is lit the same way
   every night -- a wall under a street light, not a window whose light comes and goes. On the
   control page, press **Pick colour region** and drag over the still (**Show region** shows the one
   in use), or set `number.ios2ha_camera_colour_region_x0/y0/x1/y1` (fractions of the frame).
2. Press **Set colour reference**. `sensor.ios2ha_camera_colour_reference` reads `ready`.
3. Set `select.ios2ha_camera_white_balance` to `reference`.

Each night still is then registered against the reference -- the camera may be nudged a little -- the
region is measured where the whole view says it went, and the still gets the per-channel gains that
give it its remembered colour, faded in with the night render; daylight stays as the phone shot it.
Tested on 2026-09-28's real stacks: a reference from before the curtain, applied either side of the
shift it caused, brought both to the same colours across the scene. `colour_reference` reads
`applied` when a still was corrected and `no_match` when the view no longer matches -- the camera has
moved too far: record it again. Recording is refused in daylight (a sunlit colour is not what night
stills should be held to), with no still yet, or when the region is too dark to measure.

## Levelling the view

Press **Level horizon** (`button.ios2ha_camera_level_horizon`) on a day when the last still is a
daylight one. It measures how far the view leans, adds that to `number.ios2ha_camera_rotation` and
takes a still at once to show it: stills are turned by the rotation (clockwise for positive) and
zoomed just enough that no corner shows (at 2 degrees, about 4% of the view). The live and stacked feeds are left as the phone sends them.
`sensor.ios2ha_camera_horizon` says what happened: `levelled`, or why it was refused.

The lean is read from the verticals -- building edges, window frames -- not the rooflines: on the
28 Sep stills each facade's roofline leant 3 to 8 degrees by perspective alone. The camera looks up
about 13 degrees, which fans the verticals out towards the edges, so the lean is taken where that
fan has none, at the centre. Checked on the archive: one framing read -0.31 to -0.71 degrees across
three and a half days and nights, the current one +0.71 to +0.79 by day.

It is refused:

- at night or late dusk (`rejected_night`, when the still used more than half the night render):
  verticals then include reflections in the glass, and one night still read 1.4 degrees off while
  looking certain of it. A grey day's little night weight does not count;
- with too few clear verticals, or ones that disagree (`rejected_no_clear_lines`);
- before the first still (`rejected_no_still`).

Pressing it again changes nothing more than the first press did: the lean is measured on the still
as shown and added to the rotation that still was turned by, so a second press before the next still
gives the same answer. Set the rotation by hand to undo it. The black level, the gains and the
sharpness and clipping readings are taken from the stack before it is turned, so setting a rotation
does not shift them.

A colour reference records the region as drawn on the levelled still. **Record it again after
changing the rotation**: a reference from before keeps being applied -- a small turn still
correlates -- but measured slightly off, which moves its gains by a few per cent.

## Removing the sensor's fixed grain

Stacking removes only what changes from frame to frame. Measured 2026-09-28, at a night stack depth
of ~600 frames the grain left in the dark sky was about 90% a pattern fixed to the sensor's pixels:
two independent half-stacks matched at 0.91, and two captures 25 minutes apart and at different focus
at 0.92. No depth can touch that, and the smoothing that hid it is what painted the sky into blotches.

It can be recorded once and subtracted from every night still. **A dark frame does not work**: with
the lens covered, iOS clips everything below black before encoding, so the pattern is flattened away
(it matched the sky's at 0.07). It shows only with a little light on every pixel, so record a
**flat field**:

1. At night, cover the lens with a tissue or thin paper so the phone sees a dim, even glow.
2. Press **Record flat field**. It gathers about a minute of frames after the press.
3. Uncover the lens. `sensor.ios2ha_camera_flat_field` reads `ready`, or says why it refused:
   `rejected_not_flat` (the lens was not covered: there are edges in the picture), `rejected_too_dark`
   (too little light gets through: the pattern is under the black clamp), `rejected_too_bright` or
   `rejected_no_frames`. A refusal keeps the previous recording.

Recorded on 2026-09-28, the tissue's pattern matched the night sky's at 0.85, and subtracting it at
the fitted scale (`sensor.ios2ha_camera_flat_field_scale`, about 0.9) left 62% of the grain. The
smoothing is then scaled by the same share, so the sky comes out cleaner than before with the
detail the full smoothing used to soften. It applies only at night, fading out with the night render.

**Covering the lens throws the phone's white balance.** Stills after the recording on 2026-09-28
were cyan -- green/red 1.48 against 0.97 -- because the camera adapted to the tissue, and at night
there is too little light for iOS to find its way back. Restarting the app's capture session makes
it look again, so the service backgrounds and foregrounds the app after every recording attempt.
If stills ever turn cyan after anything else covers the lens, the same cycle fixes it: set
`background_below_pct` above the battery level for one capture, or wait for daylight.

## Timelapses

Since 2.16.0 the service builds timelapses itself, from its own archive, on request. The archive
keeps every still under `ARCHIVE_DIR` by date and time and is never pruned.

**Building one.** The quick way is a button: `button.ios2ha_camera_build_timelapse_12h`, `_24h`,
`_7d` or `_1mo` builds that rolling range and replaces `last_<range>`. Anything more is the
`build_timelapse` action, whose value is a JSON spec:

| key | default | meaning |
|---|---|---|
| `range` | -- | `12h`, `24h`, `7d` or `1mo` (30 days), ending now. Give one of `range`, `duration` and `start`. |
| `duration` | -- | Any span ending now (or at `end`): `50h`, `90m`, `3d`, `2w`, `1mo`. Ending now, it is rebuilt as `last_<duration>` unless named; with picks the name says so too: `last_7d_at_0600`, `last_1mo_every_24h`, `last_1mo_sunrise`, `last_1mo_sunset_after_30m`. |
| `start`, `end` | `end` = now | A span of your own, ISO 8601. A time without a zone is the service's local time (UTC in the container), so give the zone: `2026-09-28T18:00+01:00`. |
| `name` | `custom_<start>_<end>` | For a span of your own: up to 48 of `a-z 0-9 _ -`, not starting `last_`. |
| `at` | -- | Only the still nearest each of these times of day: `"06:00"`, `"06:00, 18:00"` or a list. |
| `interval` | -- | Only the still nearest each step from the start: `"24h"`, `"1h"`. |
| `sun` | -- | Only the still nearest each day's `sunrise`, `sunset` or `noon` (solar). Needs `number.ios2ha_camera_latitude` and `_longitude`. |
| `sun_offset` | 0 | Shift the sun's time: `"+30m"`, `"-1h"`. |
| `tolerance` | 15m (half the interval if less) | How far from a picked time a still may be; a day with none that close is left out. |
| `fps` | chosen | Frames a second, 1-60. See below. |
| `width` | the stills' | Width in pixels, even, 160-3840. |
| `every` | chosen | Use every Nth still (after `at`/`interval`/`sun`), 1-1000. Given, more than 20 000 stills is refused with the `every` that fits. |
| `quality` | 23 | x264 CRF, 15-40: lower is sharper and bigger. |

Only one of `at`, `interval` and `sun`. They take the still *nearest* each time, so capture times
drifting over the days cannot shift them the way every Nth still does. `{"duration": "1mo", "sun":
"sunrise"}` is a month of sunrises; `{"duration": "7d", "at": "06:00"}` a week at 6 am;
`{"duration": "1mo", "interval": "24h"}` one still a day.

**How fast.** Left out, `fps` and `every` are chosen for **about ten seconds first, then at least
20 fps**, up to 30: a span with enough stills plays 8-12 s at 20-30 fps, thinned with `every` as
needed and as close to 10 s as it can; fewer than 160 stills cannot make ten seconds at 20 fps, so
they play slower for ten seconds exactly. At one still in ten minutes: 12h is 7.2 fps, 24h 14.4 fps,
7d every 4th at 25 fps, 1mo every 16th at 27 fps, a month of sunrises 3 fps -- each about 10 s. A
given `fps` or `every` is kept and the other fitted to it. A shorter capture interval gives the
short ranges more stills, and so more frames a second, at the cost of the phone streaming more
and the archive growing faster.

Every interface sends the same thing to the same place:

```sh
# the API (or the control page's timelapse tab, which builds this form from /objects)
curl -X POST http://192.168.1.4:8099/api/v1/control/build_timelapse \
     -H 'Content-Type: application/json' -d '{"value": {"range": "7d", "fps": 24}}'
# MQTT
mosquitto_pub -t ios2ha_camera/set/build_timelapse -m '{"start": "2026-09-28T18:00+01:00", "end": "2026-09-29T07:00+01:00", "name": "storm"}'
```

From Home Assistant with the native component, the same action is `ios2ha_camera.build_timelapse`.
The API refuses a spec it cannot build with the reason. Whatever happens after that -- and anything
MQTT delivered, which has no reply -- is on `sensor.ios2ha_camera_timelapse`: its state is the
outcome and its attributes say which request and why, in words (`name`, `control`, `result`,
`reason`, `ts`). That is state, so every interface sees it; the MQTT event repeats it.

**What happens.** Builds run one at a time at the lowest CPU priority on two threads, so stacking
keeps the host; up to four wait behind. `sensor.ios2ha_camera_timelapse` says what the last request
came to -- `queued`, `building`, `built`, `deleted`, or `rejected_invalid`, `rejected_busy` (four
already waiting), `rejected_duplicate` (that name is already waiting or building),
`rejected_unknown` (deleting a name that does not exist), `failed_no_stills` (nothing archived in the
span), `failed_encoder` (ffmpeg's message is the reason), or `off` with `TIMELAPSE_DIR` empty. `sensor.ios2ha_camera_timelapse_progress`
is the build's progress in per cent. A damaged still is left out rather than failing the build.

**Watching one.** `sensor.ios2ha_camera_timelapses` counts them; its attribute `items` is the
catalogue: each by name with its span, frames, frame rate, length, size and `url`. (Under one key,
so a timelapse named `icon` cannot become the sensor's icon.) The videos are served at
`/api/v1/timelapse/<name>.mp4` (H.264, with Range so players can seek), on the media port like the
still, so they are there with `EXPOSE_API` off. The control page has a timelapse tab; the native
component lists them in Home Assistant's Media panel and its card plays them. Delete one with the
`delete_timelapse` action, `{"name": "storm"}`. Range builds are replaced on each rebuild; your own
spans are kept until you delete them. They live in `TIMELAPSE_DIR` as `<name>.mp4` and a `<name>.json`
sidecar.

**Collecting stills in Home Assistant instead.** If you still want an automation per still, trigger
on `sensor.ios2ha_camera_last_still`, which changes once per still and at no other time, with the
condition `{{ trigger.from_state is not none and trigger.from_state.state != 'unavailable' and
trigger.to_state.state not in ('unknown', 'unavailable') }}`: a change from `unavailable` is the
component replaying the old value as it reloads, and a change from `unknown` is the first still after
a restart. Do not test how recent the timestamp is -- it is when the capture *started*, and a night
still takes minutes to stack -- and do not trigger on the MQTT topic `ios2ha_camera/event`: with
`EXPOSE_MQTT=false` nothing is published there, which is how a timelapse built on it stopped on
2026-09-18 without a single error.

## What the phone will and will not do

All measured on the device, not inferred. These are the constraints most of the design exists to
work around.

- **Streaming only starts from the in-app button.** No ONVIF call starts it, and there is no
  automation path. After a relaunch or a phone reboot, someone must tap Start streaming. That is
  why the offline alert names the fix in its message.
- **The app binds to the IP it had at launch**, so the address must be fixed and the app restarted
  if it ever changes.
- **Focus is the only imaging control that works, and both halves of it do: the mode and an
  absolute lens position.** Measured 2026-09-28 on a night skyline through a window, averaging 40
  frames per step and reading the position back with `GetStatus` each time: edge detail was 0.39 at
  position 0.60, 0.88 at 0.68 (where night autofocus had put it), **4.4–4.7 at 0.78–0.82**, and
  0.79 at 1.0. So the lens moves, 1.0 is the far end of its travel rather than infinity, and night
  autofocus can sit well off the sharpest position. An earlier note here said MANUAL made things
  worse and that absolute moves did nothing. That test commanded only near and 1.0, which on this
  phone are two similar blurs, and scored them on a stacked night still whose sharpness figure is
  mostly noise — so it looked like a lens that did not move. See `focus_position` in
  [CONFIGURATION.md](CONFIGURATION.md) for holding focus.
- **Brightness is accepted and ignored**, exposure is absent, and `SetVideoEncoderConfiguration`
  returns `ActionNotSupported`. Frame stacking is the substitute for exposure control.
- **Never port-scan the phone or churn connections.** A full scan wedged a previous app until a
  manual restart.
- **The developer disk image does not survive a phone reboot.** Screenshot, relaunch and the running
  check mount it on demand; battery does not need it and keeps working regardless.
- **Frame rate is 11–14.5 fps** at 1080×1920, JPEG quality 91. Compression is not the limiting
  factor on quality; light is.

## Power and heat

This installation runs from a **3 W USB host port** (`AdapterDetails.Watts == 3`, 500 mA), which is
marginal. Measured both ways:

| State | Battery | Temperature |
|---|---|---|
| Streaming with a client attached | Falling, ~18%/hour | Rising to ~39 °C |
| Camera app closed | Charging, +322 mA | Falling to ~33 °C |

These figures are for a connection held open continuously, which the on-demand connection no longer
does by default: a night capture only holds it for roughly 60–75 seconds of every 600, and the idle
probe in between costs about 2–3 mA a minute rather than the ~300 mA of an open connection — see
"Expected duty cycle" above. Turning `switch.ios2ha_camera_on_demand` off returns to the continuous
figures in this table.

So the camera app is the dominant load and the port cannot sustain it. iOS also throttles charging
above roughly 35 °C (`AppleChargeRateLimitIndex`), which makes it self-reinforcing: hot, so charge
slower, so run on battery, so hotter.

Things that help, in order of leverage:

1. **A powered USB hub** between host and phone — keeps usbmux data while supplying 2.4 A instead of
   500 mA.
2. **Screen Curtain** (VoiceOver) — Auto-Lock is Never, so the OLED is lit 24/7 for nothing.
3. **Shade from direct sun.** A window in July is a far bigger thermal input than the phone itself.
4. **A lower capture resolution** in the app, if it offers one.
5. **A newer battery**, eventually: this cell is at 80% of design capacity after 1305 cycles, and an
   aged cell runs hotter for the same work.

The battery current sensor is the one to watch. `charging` only means "connected".

## When something is wrong

**No stills, `stream_state` is `streaming_off`.** The app answered 503: streaming is off. Open
Guardian Eye and tap Start streaming.

**`stream_state` is `stalled`.** The app's capture has hung while its HTTP server stayed up: every
connection gets one frame and then silence. In the app this looks like streaming with a black
screen and a loading ring. Stop and start streaming in the app, or relaunch it. Stills taken
meanwhile are stacked from a handful of frames, so expect `short: true`. It was seen once, on
2026-09-14, after a healthy night; the app turned streaming off by itself about ten minutes later.

**No stills, `stream_state` is `unavailable`.** The phone is not answering at all — check it is
awake, on the network, and that the app is running. The relaunch button can start the app but
cannot start streaming.

**Stills, but `short: true` in the event.** Fewer frames were available than the depth asked for.
Usually the stream dropped inside the capture window; check `frame_rate` and `last_frame`.

**Everything looks healthy but Home Assistant shows nothing.** Check the broker, then look for a
`still:` line in the log. If stills are being produced and archived, the supervisor is fine and the
problem is between it and Home Assistant.

**A still that looks wrong.** `depth_used`, `subject_level`, `wb_gain_*` and `black_level_applied_*`
report what was actually applied to that image, not what was requested — start there.
