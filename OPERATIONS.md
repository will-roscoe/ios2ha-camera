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

Host and paths default to this installation and can be overridden with `IOS2HA_HOST`,
`IOS2HA_SRC` and `IOS2HA_STACK`.

A source-only rebuild takes about 4 seconds: dependencies are installed from `pyproject.toml`
against a stub package first, so the ~100-package layer is cached and a code change does not
reinstall opencv and numpy. The provenance build args come last in the Dockerfile for the same
reason.

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

`stop_grace_period: 20s` matters. Worst case is about 17 seconds: up to 4 s for the event loop to
finish or cancel a `/live` viewer's pending write, 5 s for the upstream connection to close, two 3 s
thread joins, and up to 2 s to publish "offline" over MQTT — see [ARCHITECTURE.md](ARCHITECTURE.md)
for the full arithmetic. Measured end to end with a viewer attached: 1.1–5.1 s in practice, well
inside the budget. Before the on-demand connection, the same shutdown could take 122 s with a viewer
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

A downgrade to a version before 1.1.0 needs one extra step. `stream_state` gained a new option,
`idle`, in 1.1.0; an older version's Home Assistant discovery payload does not list it, so a
retained `idle` value left over from the newer version is not one of the older enum's valid options
and Home Assistant will show the sensor as unavailable until it changes. The older version's health
tick republishes its own real state within 30 seconds regardless, which clears this on its own — but
to fix it immediately, clear or republish the retained `ios2ha_camera/state/stream_state` topic by
hand after the downgrade.

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

## Health and alerts

Availability is a last-will topic plus frame age. With the connection on demand, `camera_online` is
true while `streaming` with a frame within 30 seconds, **or** while `idle` with a frame within 150
seconds — idle is the normal state whenever nothing is capturing or watching, so it must not read as
offline on its own. It is forced **false** whenever the upstream thread's own heartbeat
(`sensor.ios2ha_camera_tick_age`) has gone more than 180 seconds without ticking, regardless of what
state it last reported — a wedged or dead thread must not leave every reading frozen at whatever it
last saw as healthy.

**The idle probe.** While nothing holds the connection, it is probed every 60 seconds — connect, read
one frame, close, for about 2–3 mA a minute — which is what keeps `idle` fed with a recent frame and
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

Five alerts are worth having, and all five exist:

| Alert | Trigger | Why |
|---|---|---|
| Offline | `camera_online` off or unavailable for 5 min | The original failure; unavailable means the supervisor itself stopped reporting |
| Losing charge | Battery current negative for 20 min | The supply cannot keep up; invisible in "charging". Check `sensor.ios2ha_camera_holders` and `sensor.ios2ha_camera_duty_1h` first — a connection stuck open (a viewer that never left, or `on_demand` switched off) looks exactly like a weak charger on the battery current alone |
| Battery low | Below 40% | It will stop capturing when the phone dies |
| Battery hot | Above 42 °C for 10 min | Charges and streams continuously |
| Archive space low | Below 5 GB | Archiving stops at 2 GB |

A caveat learned the hard way: Home Assistant will report a notification as delivered whether or not
your phone ever receives it. If the companion app has lost its connection, `notify` still succeeds.
Test the whole path occasionally rather than trusting that an alert exists.

## What the phone will and will not do

All measured on the device, not inferred. These are the constraints most of the design exists to
work around.

- **Streaming only starts from the in-app button.** No ONVIF call starts it, and there is no
  automation path. After a relaunch or a phone reboot, someone must tap Start streaming. That is
  why the offline alert names the fix in its message.
- **The app binds to the IP it had at launch**, so the address must be fixed and the app restarted
  if it ever changes.
- **Focus mode is the only imaging control that works — and `MANUAL` makes things worse.** Measured
  with focus sharpness on a night scene: AUTO 59.7, MANUAL 19.4–23.1 whatever position is
  commanded. The imaging service *advertises* absolute focus moves and **accepts them without a
  SOAP fault**, but near and infinity produce the same sharpness, so the lens is not moving. Leave
  autofocus on.
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

**No stills, `stream_state` is `unreachable`.** The phone is not answering at all — check it is
awake, on the network, and that the app is running. The relaunch button can start the app but
cannot start streaming.

**Stills, but `short: true` in the event.** Fewer frames were available than the depth asked for.
Usually the stream dropped inside the capture window; check `frame_rate` and `last_frame`.

**Everything looks healthy but Home Assistant shows nothing.** Check the broker, then look for a
`still:` line in the log. If stills are being produced and archived, the supervisor is fine and the
problem is between it and Home Assistant.

**A still that looks wrong.** `depth_used`, `subject_level`, `wb_gain_*` and `black_level_applied_*`
report what was actually applied to that image, not what was requested — start there.
