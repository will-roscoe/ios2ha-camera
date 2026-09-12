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

`stop_grace_period: 20s` matters. Shutdown can take about 14 seconds worst case — the ingest thread
may be waiting out a connect to an unresponsive phone before the offline availability is published —
and Compose's default 10 s would SIGKILL it mid-shutdown, leaving Home Assistant showing the camera
as online after a planned restart.

### The MQTT account

The Mosquitto add-on has no local logins, so it authenticates **Home Assistant** users. Create a
dedicated non-admin, local-only account rather than reusing an admin one.

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

Availability is a last-will topic plus frame age. `camera_online` requires both `streaming` **and**
a frame within 30 seconds, so a frozen app that holds the connection open still reads as offline.

Five alerts are worth having, and all five exist:

| Alert | Trigger | Why |
|---|---|---|
| Offline | `camera_online` off or unavailable for 5 min | The original failure; unavailable means the supervisor itself stopped reporting |
| Losing charge | Battery current negative for 20 min | The supply cannot keep up; invisible in "charging" |
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
