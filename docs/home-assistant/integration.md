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
There is no separate port or path prefix to configure. `EXPOSE_API` covers the
JSON routes in this section only — the media paths in the next section are
served unconditionally, whatever either flag is set to.

**There is no authentication.** Anything that can reach the port can read every
value the supervisor knows and write every control it exposes. Treat the port
exactly as you would an unauthenticated MQTT broker: reaching it from beyond a
trusted network — a port forward, a tunnel, anything — hands out every value and
every control with no login in front of it.

`GET /api/v1/info` is the only path worth hardcoding. It reports the running
version, `{"api": 1, "mqtt": 1}` interface versions, the device block, an
`objects_version`, and a `routes` map naming everywhere else:

| Route | Method | What it serves |
|---|---|---|
| `objects` | `GET` | `objects_version`, then every entity descriptor (`objects`) and every media descriptor (`media`) — see [Entity descriptors](#entity-descriptors) |
| `state` | `GET` | Every current value, keyed by object id |
| `control` | `POST` to `/api/v1/control/{name}` | One verb for every writable entity: `{"value": ...}` in, `{"ok": true or false, ...}` out |
| `media` | `GET` | The same media descriptors `objects` embeds, on their own |
| `events` | `GET` | Server-Sent Events: a full snapshot on connect, then only the values that changed — see [Events](#events) |

A `control` response of `200` means the write was accepted and handed to the
supervisor, not that it has taken effect — read `state` back to confirm, the same
way a Home Assistant automation would watch an MQTT state topic settle. A rejected
write (an out-of-range number, an option outside a select's list, an unknown or
read-only name) comes back as a `4xx` with an `error` string and, where it makes
sense, the value currently in effect.

A button that works the phone (`screenshot`, `relaunch_app`, `prepare_phone`,
`start_streaming`) runs at most once at a time. A press that arrives while the
same action is already queued or running is still answered `200`, but is folded
into the one in play rather than queued again, so pressing twice does not do the
work twice. A screenshot pressed during a screenshot therefore returns the image
that was already being taken. A write that carries a value, such as `autofocus`,
is not folded away: the latest value asked for is applied once the change in
play finishes.

`objects_version` is a fingerprint of the whole `objects` document, served as
`"sha256:"` followed by 64 hex digits. It is the same string in `info`, at the
top of `objects` and in every event-stream `snapshot`, and it changes whenever
any descriptor does. It exists so a client that has cached the descriptors —
across a reconnect or a Home Assistant restart — can check them against one
small `info` request instead of refetching every descriptor: if the string
matches its cache, the cache is current. Compare it for equality only; its
contents are not meant to be read.

## Entity descriptors

`objects` holds one descriptor per entity, and every descriptor carries all
fifteen keys below, with `null` where a key does not apply to that entity —
never a missing key. Key a descriptor on the pair (`domain`, `object_id`):
`object_id` alone is not unique, since `screenshot` is both a camera and a
button. The entities themselves are listed in {doc}`entities`.

| Key | Type | Meaning | `null` when |
|---|---|---|---|
| `object_id` | string | The entity's id within its domain; also the `{name}` a `control` write uses | never |
| `domain` | string | The Home Assistant platform: `camera`, `number`, `select`, `switch`, `button`, `sensor` or `binary_sensor`. More may be added; skip a domain you do not handle | never |
| `name` | string | Display name, without the device name | on the one entity that is the device's main feature (the `still` camera), which Home Assistant then names after the device alone |
| `state_key` | string | The key under which the entity's value appears in `state` and in `state` events | the entity has no value: cameras and buttons |
| `device_class` | string | Home Assistant device class, such as `battery` or `enum` | the entity has none |
| `unit` | string | Unit of measurement, such as `%` or `W` | the value has no unit, and always for cameras, selects, switches, buttons and binary sensors |
| `state_class` | string | Home Assistant state class; `measurement` today | the value is not a measurement for statistics, and always outside `sensor` |
| `options` | list of strings | Every value the entity can hold | the entity is not a `select` or an `enum` sensor |
| `lo`, `hi` | number | Inclusive bounds a `number` accepts | the entity is not a `number` |
| `step` | number | The increment a `number` moves in | the entity is not a `number` |
| `category` | string | Home Assistant entity category, `config` or `diagnostic` | the entity is a primary one, shown without a category |
| `icon` | string | A Material Design icon, such as `mdi:battery-arrow-down` | Home Assistant's default for the domain and device class should be used |
| `writable` | boolean | Whether `control` accepts it; true for every `number`, `select`, `switch` and `button` | never |
| `precision` | integer | Digits to display, as Home Assistant's suggested display precision | the entity does not set one, and always outside `sensor` |

A button has no value to send: a `control` write to it is a press, and any
`value` in the request is ignored. `lo`, `hi` and `step` may be integers or
floats, so read them as numbers rather than as either. As with every other part
of the API, a descriptor may gain keys; ignore any you do not recognise.

## Events

`GET /api/v1/events` is a `text/event-stream` that stays open. One connection is
enough for every entity: there is nothing to poll.

```
event: snapshot
data: {"objects_version": "sha256:...", "state": {"phone_battery": 81, ...}}

event: state
data: {"state": {"phone_battery": 80}}

event: objects_changed
data: {"objects_version": "sha256:..."}

: keepalive
```

The `snapshot` arrives first and carries every value, exactly as a reconnecting
MQTT client gets the retained topics, so no separate `GET /api/v1/state` is
needed. After it, a `state` event carries only the keys whose value changed; a
value republished unchanged sends nothing. `objects_changed` means a cached
`objects` document is stale and should be refetched — comparing the
`objects_version` in a snapshot with the cached one catches the same thing across
a restart. The comment line is a keepalive, sent every 20 seconds or so, because
a proxy will close a connection that says nothing for long enough.

A client that stops reading is disconnected rather than being sent a partial
stream: a missed delta would leave it displaying a wrong value with nothing to
tell it so, where a disconnect is visible and a reconnect brings a fresh
snapshot. Reconnect and carry on.

## Media

`objects`'s `media` list, and `GET /api/v1/media` on its own, describe the same
streams and still that `/live`, `/live/stacked` and `/still.jpg` already serve —
see {doc}`../guarantees` for why those three paths are permanent. The
`holds_phone` field is the one worth reading before building a dashboard: the
still costs the phone nothing to fetch, while either stream holds its connection
open for as long as something is watching.

**The six media paths themselves — the three above, plus their `/api/v1/stream/live`,
`/api/v1/stream/stacked` and `/api/v1/snapshot/still` equivalents — are not
gated by `EXPOSE_API`.** They are served unconditionally, because they are the
camera; only the JSON routes in the section above turn off with the flag.
Setting `EXPOSE_API=false` does not close any `/api/v1/stream/...` or
`/api/v1/snapshot/...` path, and none of the six carries authentication either
way.

## Moving from MQTT to the component

The component is meant to take over the same entity ids MQTT discovery created,
`<domain>.ios2ha_camera_<object_id>`, so dashboards and automations keep working.
Home Assistant will not hand an id to a second entity while the first still holds
it, so the order matters:

1. **Turn MQTT off.** Set `EXPOSE_MQTT=false` and restart the supervisor. At
   startup it clears its retained discovery topics and its retained availability
   message, which deletes the MQTT entities and releases their ids (see
   {doc}`../supervisor/configuration`). Check the entities have gone from Home
   Assistant before carrying on; if the broker was unreachable, the log says so
   and nothing was cleared.
2. **Install the component** and add the supervisor. Its entities take the same
   object ids, and so the same entity ids.
3. **Remove any hand-made Generic Camera entries** pointed at `/live`,
   `/live/stacked` or `/still.jpg`, once the component's cameras are working.
   Until then they keep working: those paths are permanent.

## Versioning

Covered in full in {doc}`../guarantees`. In short: read `interfaces.api` from
`/api/v1/info`, ignore any key you do not recognise, and a newer ios2ha-camera
supervisor will never break your integration — within `api: 1` the JSON API only
grows.
