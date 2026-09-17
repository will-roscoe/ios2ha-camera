# Installation

## Before you start: the phone

The prerequisites that are easiest to get wrong, and that make the service look
broken when they are missing:

- Background App Refresh **on**. Without it iOS kills the camera app rather than
  suspending it, and the app has to be restarted by hand.
- Auto-Lock set to **Never**.
- A fixed IP address. The app binds to whatever address it had at launch, so a DHCP
  change means restarting the app.
- The app's own quality settings, which materially change both image quality and
  the power the phone draws.

These four are enough to get frames flowing over WiFi. Two further one-time steps
matter only if you want the cable-based features covered below — the service works
without them, just without those extras:

- **No passcode** (Settings → Face ID & Passcode → Turn Passcode Off). A passcode
  blocks USB entirely after a restart, so nothing over the cable works at all —
  not battery reporting, not relaunching the app, not the keyboard.
- **Bluetooth on, Full Keyboard Access on** (Settings → Accessibility →
  Keyboards), and the host paired once from Settings → Bluetooth — needed only for
  the auto-start keyboard described under "The USB cable and the Bluetooth
  keyboard are optional" below.

Also worth knowing before you start: focus mode is the only imaging control that
actually works on the tested app, and autofocus should be left on — manual focus
makes sharpness worse, not better. There is nothing to configure here; it only
matters if you go looking for an exposure or focus control and wonder why one
isn't offered.

## Docker Compose alongside other services

This is how the service is actually run day to day: as one more service in a
larger compose file, on the same host as the MQTT broker it talks to. A generic
version of that service block looks like this:

```yaml
services:
  ios2ha-camera:
    build: ./ios2ha-camera        # path to a checkout of this repository
    container_name: ios2ha-camera
    network_mode: host
    environment:
      - PHONE_HOST=${IOS2HA_PHONE_HOST}       # e.g. 192.0.2.10 -- must be a fixed address
      - PHONE_PASSWORD=${IOS2HA_PHONE_PASSWORD}
      - PHONE_BUNDLE_ID=com.guardianeye.webcam.ios
      - MQTT_URL=mqtt://broker.lan
      - MQTT_USER=${IOS2HA_MQTT_USER}
      - MQTT_PASSWORD=${IOS2HA_MQTT_PASSWORD}
      - ARCHIVE_DIR=/data/archive
      - LIVE_PORT=8099
      - LOG_LEVEL=INFO
      - DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/bluez-proxy/bus
    volumes:
      - ./data/ios2ha-camera:/data
      - /var/run/usbmuxd:/var/run/usbmuxd
      - /var/lib/lockdown:/var/lib/lockdown:ro
      - /run/bluez-proxy:/run/bluez-proxy
    restart: unless-stopped
    stop_grace_period: 30s
```

Bring it up the same way as anything else in the file:

```bash
docker compose up -d ios2ha-camera
```

A few things in there need explaining, because getting them wrong is invisible
until something restarts:

**`network_mode: host`.** The service has to reach three things directly: the
phone over WiFi, the MQTT broker, and the local USB socket. Running it on a
bridge network would need explicit routes to all three; host networking is
simpler and is what this project is built and tested against.

**`/var/run/usbmuxd:/var/run/usbmuxd`.** This is the socket `usbmuxd` on the host
listens on for cable access — battery readings and the app-control features
described below. It's bind-mounted as a path rather than a named volume because
it has to resolve to the *same* file the host's `usbmuxd` is currently serving.
If `usbmuxd` restarts (which happens on every cable reattach), it creates a new
socket inode, and the container goes on holding the old, deleted one — every
cable operation then fails until the container is recreated. See "When the cable
stops working" in {doc}`operations` for the host-side fix
(`scripts/install-usbmuxd-hook.sh`) and the container's own slower fallback.

**`/var/lib/lockdown:/var/lib/lockdown:ro`.** Read-only pairing records the phone
needs for the battery and app-control features. Nothing is written back to it.

**`/run/bluez-proxy:/run/bluez-proxy`** (plus `DBUS_SYSTEM_BUS_ADDRESS`). This is
for the Bluetooth keyboard feature — see the optional-features section below for
what it's for and how to set it up. Mount the *directory*, not the socket file
inside it: the proxy recreates the socket on restart, and a bind-mounted socket
file would be orphaned the same way the `usbmuxd` socket is above.

**`stop_grace_period: 30s`** and **`restart: unless-stopped`**. Worst-case clean
shutdown is measured at 20 seconds (event loop teardown, closing the upstream
connection, stopping the keyboard, publishing MQTT's offline state, and — if one
is in flight — waiting for an in-progress cable or ONVIF call to finish). 30s
leaves 10 s of margin over that; don't set it lower; a container killed
mid-shutdown leaves Home Assistant showing the camera as online when it isn't.
`restart: unless-stopped` matters because a plain `docker restart` — or a full
recreate — is also the manual fix for the cable fault above, and because the
container's own cable self-heal relies on being restarted by Docker after it
exits.

The environment variables above are only the ones worth calling out here. For
the full list, including what's required versus optional and what each tier
(Core, MQTT, API) gates, see {doc}`configuration`. Notably, with neither
`MQTT_URL` nor the deprecated `MQTT_HOST` set, the broker defaults to
`mqtt://localhost` — with host networking, a broker on the same machine. The
example above names one explicitly, because a broker elsewhere is just as
common.

### The USB cable and the Bluetooth keyboard are optional

Both `/var/run/usbmuxd` and the Bluetooth keyboard proxy are optional features,
not requirements for a working camera:

- **Without the cable** (no `usbmuxd` mount, no `lockdown` mount), the service
  still streams and stacks frames over WiFi as normal. You lose battery
  reporting and the ability to relaunch or foreground the app automatically —
  if the app dies or gets backgrounded, someone has to bring it back by hand.
- **Without the Bluetooth keyboard proxy**, the service can still open the app
  over the cable, but cannot press the in-app "Start streaming" button itself —
  there's no ONVIF call or API for it, so after the phone reboots or the app is
  cold-started, someone has to tap it once. The keyboard exists purely to press
  that one button automatically. Without the proxy set up, this reports as
  `failed_keyboard_not_connected` and everything else carries on unchanged.

To set the keyboard proxy up, on the Docker host (not in the container), run:

```bash
sudo ./scripts/install-bluez-proxy.sh
```

This installs and starts `bluez-dbus-proxy.service`, a filtered D-Bus proxy that
gives the container access to `org.bluez` and nothing else on the host's system
bus — not systemd, not login1. It needs `xdg-dbus-proxy` installed
(`apt install xdg-dbus-proxy`). The script verifies the filter itself: it checks
BlueZ answers through the proxy and that systemd does not. `./deploy.sh check`
reports whether the proxy is present on a deployed host.

On the phone side, the keyboard feature additionally needs Bluetooth and Full
Keyboard Access switched on and the host paired once, as listed above — it
appears in the phone's Bluetooth settings under the host's own Bluetooth name,
not any name specific to this service.

### First run, and after every phone restart

Streaming only ever starts from the in-app button — there's no ONVIF call or
API for it — so the first time the container comes up, and again any time the
phone has restarted, someone has to open the camera app and tap Start
streaming at least once by hand. With the cable and keyboard set up as above,
you can drive that from the host instead of picking up the phone:

```bash
docker compose run --rm --no-deps ios2ha-camera python -m ios2ha_camera.phone_setup
```

This checks the phone is reachable over the cable, relaunches the app if it
isn't running, reads the battery, and then waits (up to five minutes) for
someone to tap Start streaming, confirming with the same probe the service
itself uses that frames are actually arriving. See {doc}`operations` for what
each step does and for `--fix-developer-mode`, which also exists but reboots
the phone, so it's opt-in only.

## Standalone Docker

The same image with no other services around it — useful for testing, or a host
that only ever runs this one container. Build and run it directly:

```bash
docker build -t ios2ha-camera .

docker run -d \
  --name ios2ha-camera \
  --network host \
  --restart unless-stopped \
  --stop-timeout 30 \
  -e PHONE_HOST=192.0.2.10 \
  -e PHONE_PASSWORD=changeme \
  -e MQTT_URL=mqtt://broker.lan \
  -e MQTT_USER=ios2ha \
  -e MQTT_PASSWORD=changeme \
  -v "$(pwd)/data:/data" \
  -v /var/run/usbmuxd:/var/run/usbmuxd \
  -v /var/lib/lockdown:/var/lib/lockdown:ro \
  -v /run/bluez-proxy:/run/bluez-proxy \
  -e DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/bluez-proxy/bus \
  ios2ha-camera
```

Everything said above about `network_mode: host`, the two cable-related mounts,
the Bluetooth proxy mount, and the shutdown timing applies here in the same way
— `--stop-timeout 30` is `docker run`'s equivalent of `stop_grace_period`. Drop
the `usbmuxd`/`lockdown`/`bluez-proxy` lines and their env var if you don't want
the optional cable and keyboard features (see above for what you lose).

If you'd rather build with the same provenance stamping `deploy.sh` uses,
pass the git description and build date as build args:

```bash
docker build \
  --build-arg GIT_DESCRIBE="$(git describe --always --dirty --tags)" \
  --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -t ios2ha-camera .
```

Building and running it by hand like this is fine for a one-off host. If you
manage several, or deploy the same tree repeatedly, `deploy.sh` in the
repository root does the equivalent build-and-restart over SSH and stamps the
image with `git describe`, so `./deploy.sh version` always says what's actually
running — see {doc}`operations` for what it and `./deploy.sh check` do.

## Bare Python

For development, or a host where Docker isn't wanted. Requires Python 3.12 or
newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .

export PHONE_HOST=192.0.2.10
export PHONE_PASSWORD=changeme
export MQTT_URL=mqtt://broker.lan
export MQTT_USER=ios2ha
export MQTT_PASSWORD=changeme
export ARCHIVE_DIR=./data/archive

python -m ios2ha_camera
```

This runs the same entry point the container does. It's the right way to work
on the service itself — for actually running it, use one of the Docker routes
above, which also get you the restart policy and shutdown handling described
there. The cable and keyboard features need `usbmuxd` and, respectively,
`/var/lib/lockdown` and a reachable `org.bluez` on whatever machine you run this
on; there's no container boundary to mount them through, so they either work or
don't depending on what the host already has.

## Connecting it to Home Assistant

Two ways to hook the service up once it's running, covered in full under
{doc}`../home-assistant/index`:

- **MQTT plus two camera entries** — works today, with any Home Assistant
  install. The service publishes MQTT discovery messages once it can reach the
  broker, and you add two camera entities by hand for the MJPEG streams. See
  {doc}`../home-assistant/mqtt` for the broker side of this, including why it
  needs its own broker-local login rather than a Home Assistant account.
- **The native integration** — a HACS custom component that talks to the
  service's JSON API directly, with no broker and no manual entity setup. Still
  in development; see {doc}`../home-assistant/integration` for what the service
  already serves for it.
