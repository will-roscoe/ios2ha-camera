#!/usr/bin/env bash
# Install the BlueZ-only D-Bus proxy that the container's keyboard talks through.
#
# The container emulates a Bluetooth keyboard to press the camera app's "Start
# streaming" button, which needs BlueZ. Mounting the real system bus would give
# a root container the whole bus, systemd included, so it gets this filtered
# socket instead. Run on the Docker host, as root. Idempotent.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
UNIT=/etc/systemd/system/bluez-dbus-proxy.service

command -v xdg-dbus-proxy >/dev/null || {
  echo "xdg-dbus-proxy is not installed (apt install xdg-dbus-proxy)" >&2
  exit 1
}

install -m 0644 "$HERE/bluez-dbus-proxy.service" "$UNIT"
systemd-analyze verify "$UNIT"
systemctl daemon-reload
systemctl enable --now bluez-dbus-proxy.service
systemctl is-active bluez-dbus-proxy.service >/dev/null || {
  echo "the proxy did not start; see: journalctl -u bluez-dbus-proxy" >&2
  exit 1
}

# Prove the filter rather than trusting it: BlueZ reachable, systemd not.
A=unix:path=/run/bluez-proxy/bus
busctl --address="$A" --timeout=5 call org.bluez / org.freedesktop.DBus.ObjectManager \
  GetManagedObjects >/dev/null || {
  echo "FAIL: org.bluez is not reachable through the proxy" >&2
  exit 1
}
echo "org.bluez: reachable"
if busctl --address="$A" --timeout=5 call org.freedesktop.systemd1 /org/freedesktop/systemd1 \
     org.freedesktop.systemd1.Manager ListUnits >/dev/null 2>&1; then
  echo "FAIL: systemd is reachable through the proxy" >&2
  exit 1
fi
echo "systemd: hidden. proxy ready at /run/bluez-proxy/bus"
