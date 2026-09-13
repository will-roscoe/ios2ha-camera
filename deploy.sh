#!/usr/bin/env bash
# Deploy the supervisor, stamping the running image with its provenance.
#
# The deployment is an rsync of a working tree rather than a pull of a tagged
# commit, so nothing else records what is actually running. This script bakes
# `git describe --always --dirty --tags` into the image, which means a build
# from a modified tree is visibly marked rather than quietly passing as a
# release. Deploy by hand and you lose that; use this instead.
#
#   ./deploy.sh            sync, build, restart, verify
#   ./deploy.sh check      compare the deployed tree against this one
#   ./deploy.sh version    what is running right now
#   ./deploy.sh phone      prepare the phone after it restarts (works while stopped)
set -euo pipefail

HOST="${IOS2HA_HOST:-arcturus.lan0}"
SRC="${IOS2HA_SRC:-/home/will/arcturus-stack/src/ios2ha-camera}"
STACK="${IOS2HA_STACK:-/home/will/arcturus-stack/mediastack}"
SERVICE=ios2ha-camera

cd "$(dirname "$0")"

# egg-info is a local artefact of the editable install and never syncs.
tree_hash() {
  find src tests pyproject.toml Dockerfile -type f ! -name '*.pyc' ! -path '*egg-info*' \
    | sort | xargs sha256sum | sha256sum | cut -d' ' -f1
}

case "${1:-deploy}" in
version)
  ssh -o BatchMode=yes "$HOST" "docker logs $SERVICE 2>&1 | grep -m1 'ios2ha-camera '" || {
    echo "no version line in the log; the container may predate versioning" >&2; exit 1; }
  ;;

check)
  remote=$(ssh -o BatchMode=yes "$HOST" "cd $SRC && find src tests pyproject.toml Dockerfile \
    -type f ! -name '*.pyc' | sort | xargs sha256sum | sha256sum | cut -d' ' -f1")
  if [ "$(tree_hash)" = "$remote" ]; then
    echo "deployed tree matches this one"
  else
    echo "MISMATCH: the deployed tree is not this one" >&2; exit 1
  fi
  ;;

deploy)
  describe=$(git describe --always --dirty --tags 2>/dev/null || echo unknown)
  built=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  case "$describe" in
    *-dirty) echo "WARNING: deploying a modified working tree; the image will be marked $describe" >&2 ;;
  esac
  echo "deploying $describe (built $built) to $HOST"

  rsync -a --delete \
    --exclude .git --exclude .claude --exclude .superpowers \
    --exclude __pycache__ --exclude '*.egg-info' --exclude .pytest_cache \
    ./ "$HOST:$SRC/"

  ssh -o BatchMode=yes "$HOST" \
    "cd $STACK && docker compose build --build-arg GIT_DESCRIBE='$describe' \
       --build-arg BUILD_DATE='$built' $SERVICE && docker compose up -d $SERVICE"

  echo "waiting for it to come up..."
  ssh -o BatchMode=yes "$HOST" \
    "until docker logs --since 120s $SERVICE 2>&1 | grep -q 'supervisor up'; do sleep 2; done
     docker logs --since 120s $SERVICE 2>&1 | head -3"
  ;;

phone)
  ssh -t -o BatchMode=yes "$HOST" \
    "cd $STACK && docker compose run --rm --no-deps $SERVICE python -m ios2ha_camera.phone_setup ${2:-}"
  ;;

*)
  echo "usage: $0 [deploy|check|version|phone]" >&2; exit 2 ;;
esac
