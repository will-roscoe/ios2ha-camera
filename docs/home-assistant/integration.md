# The native integration

:::{note}
The Home Assistant custom component itself is not built yet. This page describes
what the ios2ha-camera supervisor already serves for it to talk to, so the
component's design does not have to guess at the wire format, and so the
documentation structure does not have to be rearranged when it arrives.
:::

A future Home Assistant custom component, installed through HACS, that talks to
the supervisor directly over the JSON API below, instead of through a broker.

It will live in **its own repository**, not this one. HACS requires `hacs.json` and
`custom_components/<domain>/` at a repository's root, and selects a version from the
*most recent GitHub Release* in that repository, with no way to filter releases by
tag prefix. Sharing a repository would therefore let a supervisor release be offered
to integration users as an upgrade.

## The JSON API

Served on `LIVE_PORT` (see {doc}`../supervisor/configuration`), the same port
`/live` and `/still.jpg` already use, while `EXPOSE_API` is true (the default).
There is no separate port or path prefix to configure.

**There is no authentication.** Anything that can reach the port can read every
value the supervisor knows and write every control it exposes. Treat the port
exactly as you would an unauthenticated MQTT broker: reaching it from beyond a
trusted network — a port forward, a tunnel, anything — hands out every value and
every control with no login in front of it.

`GET /api/v1/info` is the only path worth hardcoding. It reports the running
version, `{"api": 1, "mqtt": 1}` interface versions, the device block, and a
`routes` map naming everywhere else:

| Route | Method | What it serves |
|---|---|---|
| `objects` | `GET` | Every entity descriptor and every media descriptor — see {doc}`entities` |
| `state` | `GET` | Every current value, keyed by object id |
| `control` | `POST` to `/api/v1/control/{name}` | One verb for every writable entity: `{"value": ...}` in, `{"ok": true or false, ...}` out |
| `media` | `GET` | The same media descriptors `objects` embeds, on their own |
| `events` | — | **Not served yet.** Named in `routes` so a client can find it the moment it exists; a request today gets a 404 |

A `control` response of `200` means the write was accepted and handed to the
supervisor, not that it has taken effect — read `state` back to confirm, the same
way a Home Assistant automation would watch an MQTT state topic settle. A rejected
write (an out-of-range number, an option outside a select's list, an unknown or
read-only name) comes back as a `4xx` with an `error` string and, where it makes
sense, the value currently in effect.

## Media

`objects`'s `media` list, and `GET /api/v1/media` on its own, describe the same
streams and still that `/live`, `/live/stacked` and `/still.jpg` already serve —
see {doc}`../guarantees` for why those three paths are permanent. The
`holds_phone` field is the one worth reading before building a dashboard: the
still costs the phone nothing to fetch, while either stream holds its connection
open for as long as something is watching.

## Versioning

Covered in full in {doc}`../guarantees`. In short: read `interfaces.api` from
`/api/v1/info`, ignore any key you do not recognise, and a newer ios2ha-camera
supervisor will never break your integration — within `api: 1` the JSON API only
grows.
