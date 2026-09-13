#!/usr/bin/env bash
#
# Reattach the ios2ha-camera container after usbmuxd restarts.
#
# THE FAULT THIS FIXES
#
# The container reaches the iPhone over the cable through usbmuxd's unix socket,
# which compose bind-mounts as a *file*:
#
#     - /var/run/usbmuxd:/var/run/usbmuxd
#
# usbmuxd on this host is udev-activated (/usr/lib/udev/rules.d/39-usbmuxd.rules;
# the unit is `static` with an empty TriggeredBy and
# WantedBy=sys-devices-...usb1-1.device) and runs with --systemd, which implies
# --enable-exit: it exits whenever no device is attached. Every start creates a
# NEW socket, with a new inode. The container is still holding the old, deleted
# inode, so every cable operation fails forever with
# ConnectionFailedToUsbmuxdError until the container is recreated. That is what
# cost two hours of battery and diagnostics data on 2026-09-13.
#
# WHAT THIS INSTALLS
#
#   /usr/local/sbin/ios2ha-camera-reattach                      a guarded helper
#   /etc/systemd/system/usbmuxd.service.d/10-ios2ha-reattach.conf  ExecStartPost
#
# The drop-in fires the helper through `systemd-run --no-block`, so usbmuxd's own
# startup is never blocked by the container's restart (which can take ~20s, since
# compose sets stop_grace_period: 20s).
#
# The helper is deliberately timid. It does nothing unless docker is up and the
# container is already running, so a deliberately stopped container stays stopped
# and a boot-time race where usbmuxd starts before docker is harmless. It also
# rate-limits itself, because udev starts usbmuxd on every attach and a flapping
# cable must not restart the container in a loop.
#
# USAGE
#
#   sudo ./install-usbmuxd-hook.sh              install (idempotent)
#   sudo ./install-usbmuxd-hook.sh --uninstall  remove everything it installed
#   sudo ./install-usbmuxd-hook.sh --container NAME    non-default container name
#
set -euo pipefail

CONTAINER="ios2ha-camera"
MIN_INTERVAL=60            # seconds between restarts, to survive a flapping cable
HELPER=/usr/local/sbin/ios2ha-camera-reattach
DROPIN_DIR=/etc/systemd/system/usbmuxd.service.d
DROPIN="$DROPIN_DIR/10-ios2ha-reattach.conf"
ACTION=install

while [ $# -gt 0 ]; do
  case "$1" in
    --uninstall) ACTION=uninstall; shift ;;
    --container) CONTAINER="${2:?--container needs a name}"; shift 2 ;;
    -h|--help)   sed -n '2,40p' "$0"; exit 0 ;;
    *)           echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "This writes to /usr/local/sbin and /etc/systemd/system; run it with sudo." >&2
  exit 1
fi

if ! systemctl cat usbmuxd.service >/dev/null 2>&1; then
  echo "usbmuxd.service not found on this host. Nothing to hook." >&2
  exit 1
fi

if [ "$ACTION" = uninstall ]; then
  rm -fv "$DROPIN" "$HELPER"
  rmdir --ignore-fail-on-non-empty "$DROPIN_DIR" 2>/dev/null || true
  systemctl daemon-reload
  echo "Removed. usbmuxd.service no longer touches the container."
  exit 0
fi

install -d -m 0755 "$DROPIN_DIR"

cat > "$HELPER" <<HELPER_EOF
#!/usr/bin/env bash
# Installed by ios2ha-camera's install-usbmuxd-hook.sh. Edit that, not this.
#
# usbmuxd has just started and created a fresh socket. The container is holding
# the previous, now-deleted inode, so it must be restarted to pick the new one up.
set -euo pipefail

CONTAINER="$CONTAINER"
MIN_INTERVAL=$MIN_INTERVAL
STAMP=/run/ios2ha-camera-reattach.stamp

log() { logger -t ios2ha-camera-reattach -- "\$*"; }

# udev starts usbmuxd on every attach. Without this, a cable with a bad contact
# would restart the container continuously.
now=\$(date +%s)
if [ -r "\$STAMP" ]; then
  last=\$(cat "\$STAMP" 2>/dev/null || echo 0)
  if [ \$(( now - last )) -lt \$MIN_INTERVAL ]; then
    log "skipped: last restart \$(( now - last ))s ago, minimum is \${MIN_INTERVAL}s"
    exit 0
  fi
fi

# During boot usbmuxd can start before dockerd is listening. Not an error.
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  log "skipped: docker is not available yet"
  exit 0
fi

# Only ever restart something that is already running: a container stopped on
# purpose must stay stopped.
running=\$(docker inspect -f '{{.State.Running}}' "\$CONTAINER" 2>/dev/null || echo missing)
if [ "\$running" != "true" ]; then
  log "skipped: container \$CONTAINER is \$running"
  exit 0
fi

echo "\$now" > "\$STAMP"
log "usbmuxd restarted; restarting \$CONTAINER to pick up the new socket"
if docker restart "\$CONTAINER" >/dev/null; then
  log "restarted \$CONTAINER"
else
  log "FAILED to restart \$CONTAINER"
  exit 1
fi
HELPER_EOF
chmod 0755 "$HELPER"

cat > "$DROPIN" <<DROPIN_EOF
# Installed by ios2ha-camera's install-usbmuxd-hook.sh.
#
# The leading "-" means a failure here never fails usbmuxd itself: reaching the
# phone over the cable matters more than the container noticing promptly.
#
# systemd-run --no-block returns immediately, so usbmuxd's startup is not held up
# by a container restart that can take ~20s.
[Service]
ExecStartPost=-/usr/bin/systemd-run --no-block --collect \\
    --description="Reattach $CONTAINER to the new usbmuxd socket" \\
    $HELPER
DROPIN_EOF

systemctl daemon-reload

echo "Installed:"
echo "  $HELPER"
echo "  $DROPIN"
echo
echo "Effective unit configuration:"
systemctl cat usbmuxd.service | sed -n '/10-ios2ha-reattach/,$p' | sed 's/^/  /'
echo
echo "To test it, with the phone attached and the container running:"
echo "  sudo systemctl restart usbmuxd && sleep 30 && docker ps --filter name=$CONTAINER"
echo "  journalctl -t ios2ha-camera-reattach -n 20"
echo
echo "To remove:  sudo $0 --uninstall"
