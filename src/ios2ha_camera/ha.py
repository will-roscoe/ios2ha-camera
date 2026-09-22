"""Read-only access to Home Assistant, for verifying a deployment.

Off unless `HA_URL` and `HA_TOKEN` are both set; nothing here runs on the normal
path, and nothing here writes.

This is the reduced version of future-work section 5, and the reduction is the
point. That section wanted the container to create camera config entries and
install automations, which needs an **admin** token -- a service holding
credentials for the whole house, to save some clicking. The native integration
has since taken the provisioning job over, so the only part still worth having is
the part that needs no privilege at all: after a deploy, did the entities come
back, and is anything unavailable? Will's ruling 2026-09-22 was to build that
half and keep the rest documented rather than delete it.

Home Assistant has no read-only tokens, so the token this is given is worth
exactly as much as the account that minted it. **Mint it from a non-admin user.**
Nothing here needs more, and a token that cannot write cannot be turned into a
way to drive the house from a camera container.
"""

import json
import logging
import os
import urllib.request

log = logging.getLogger(__name__)

# Home Assistant renders a missing integration or a dead link as `unavailable`.
# `unknown` is different and healthy: it is a control nobody has written yet,
# such as a black level override before anyone overrides it.
UNAVAILABLE = "unavailable"

TIMEOUT_S = 15


class HomeAssistant:
    """A Home Assistant instance, read-only.

    `opener` is injected so the tests can assert what would go on the wire
    without one ever reaching it.
    """

    def __init__(self, url, token, opener=None):
        self._url = (url or "").rstrip("/")
        # Private and never rendered: see __repr__. A token in a log line is a
        # token in the journal, in a paste, and in the next screenshot.
        self._token = token or ""
        self._opener = opener or urllib.request.urlopen

    def __repr__(self):
        return f"<HomeAssistant {self._url or 'unconfigured'}>"

    @property
    def configured(self):
        return bool(self._url and self._token)

    def states(self):
        if not self.configured:
            raise RuntimeError("Home Assistant access is not configured "
                               "(set HA_URL and HA_TOKEN)")
        req = urllib.request.Request(
            f"{self._url}/api/states",
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"},
            method="GET")
        with self._opener(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read())


def summarise(states, prefix="ios2ha"):
    """What a deployment check wants to know, from a /api/states response."""
    mine = [s for s in states if prefix in s.get("entity_id", "")]
    unavailable = sorted(s["entity_id"] for s in mine
                         if s.get("state") == UNAVAILABLE)
    return {
        "total": len(mine),
        "unavailable": unavailable,
        # No entities at all means the integration is gone or the prefix is
        # wrong, which is a failure however healthy the rest looks.
        "ok": bool(mine) and not unavailable,
    }


def main(argv=None, env=None, out=print):
    """`python -m ios2ha_camera.ha` -- check the entities, say what is wrong.

    Exit status is the point: 0 healthy, 1 not, 2 not configured, so a deploy
    script can act on it.
    """
    env = os.environ if env is None else env
    ha = HomeAssistant(env.get("HA_URL", ""), env.get("HA_TOKEN", ""))
    if not ha.configured:
        out("Home Assistant access is not configured: set HA_URL and HA_TOKEN "
            "(a token from a non-admin user is enough).")
        return 2
    try:
        summary = summarise(ha.states(), prefix=env.get("HA_PREFIX", "ios2ha"))
    except Exception as e:
        out(f"could not reach Home Assistant: {type(e).__name__}: {e}")
        return 1

    if not summary["total"]:
        out("no entities found: the integration is not loaded, or HA_PREFIX is wrong")
        return 1
    if summary["unavailable"]:
        out(f"{summary['total']} entities, "
            f"{len(summary['unavailable'])} unavailable:")
        for entity_id in summary["unavailable"]:
            out(f"  {entity_id}")
        return 1
    out(f"{summary['total']} entities, none unavailable")
    return 0


if __name__ == "__main__":       # pragma: no cover - thin CLI wrapper
    raise SystemExit(main())
