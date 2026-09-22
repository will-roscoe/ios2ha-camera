What is promised
================

The **Home Assistant entity and topic surface is the public interface**, because that
is what other things depend on: automations, dashboards and templates all name
entities directly.

.. list-table::
   :header-rows: 1
   :widths: 12 88

   * - Bump
     - Means
   * - MAJOR
     - An existing entity id, topic or payload shape changed or disappeared.
       Something that was working will stop working and you will have to edit
       automations.
   * - MINOR
     - New entities, topics or capabilities. Nothing that already worked breaks.
   * - PATCH
     - Fixes and internal changes with no visible surface change.

Renaming an entity's **display name is a patch**. Entity ids are pinned with
``default_entity_id`` precisely so that display names can be improved without
breaking anything.

The JSON API
------------

The ios2ha-camera supervisor also serves a JSON API, for a future Home Assistant
custom component and for anything else that would rather read HTTP than subscribe
to MQTT. ``GET /api/v1/info`` reports an ``interfaces`` object, one small version
integer per surface -- ``{"api": 1, "mqtt": 1}`` today -- because the two change
independently: restructuring an MQTT topic does not touch a JSON caller, and
reshaping the API's envelope does not touch anyone using MQTT. Welding both to one
number would raise a false alarm on whichever side did not actually change.

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Within ``api: 1``
     - Payloads are **additive only**. A key already served keeps its name, type
       and meaning; new keys, routes and entities may appear at any time without
       the version changing.
   * - A caller
     - **Must ignore keys it does not recognise.** That is what lets a component
       built against an older API stay working against a newer ios2ha-camera
       supervisor, and is why fields are added freely rather than behind
       something a caller has to opt into.
   * - ``/api/v1/info``
     - Is the **only path worth hardcoding**. Every other route -- ``objects``,
       ``state``, ``control``, ``media`` and ``events`` -- is named in that
       document's ``routes`` map, so a route moving, or a new one arriving, is a
       change to served data rather than a client release.
   * - ``/api/v1/events``
     - The Server-Sent Events stream is served and **covered by** ``api: 1`` on
       the same terms as every other route: its event names (``snapshot``,
       ``state``, ``objects_changed``) and the keys inside each event keep their
       meaning, and new event names or keys may appear. A client must ignore an
       event name it does not recognise, just as it ignores an unknown key. The
       wire format is described in :doc:`home-assistant/integration`.

The pre-existing media paths -- ``/live``, ``/live/stacked`` and ``/still.jpg`` --
are **permanent aliases**, not a deprecated route scheduled for removal. They are
served by the same handlers as the API's own stream and snapshot routes, so a Home
Assistant Generic Camera entry pointed at one of them keeps working whether or not
anything else here ever talks to the API.

The JSON API has **no authentication**. Anything that can reach it can read every
value the supervisor knows and write every control it exposes. Treat it exactly as
you would an unauthenticated MQTT broker: reaching it from beyond a trusted
network -- a port forward, a tunnel, anything -- hands out every value and every
control with no login in front of it.

Not covered by this promise
----------------------------

- **The Python API in** ``ios2ha_camera`` -- module and function names, internal
  data structures, anything not reachable over MQTT or the JSON API. These can
  change in a patch release.
- **The log format.**
- **The on-disk archive layout** under ``ARCHIVE_DIR``.
- **The internal tuning constants** documented in :doc:`supervisor/configuration`
  (``PROBE_INTERVAL_S`` and the rest) -- read alongside the code that justifies
  each one, not a dashboard control, and free to change without notice.
- **Authentication on the JSON API or the media routes.** Neither exists yet.
  Nothing here should be read as a claim that either is safe to expose beyond a
  trusted network.
- **mDNS, or any other automatic discovery** of the ios2ha-camera supervisor's
  address. Not built; the address is entered by hand today, in Home Assistant or
  in the component's config flow.

Finding the running version
---------------------------

Three places, and they agree:

* the first line of the container log
* ``sw_version`` on the Home Assistant device page
* ``./deploy.sh version``

Each carries the git description as well as the number, so an image built from a
modified tree reads as ``1.0.0-dirty`` rather than passing as a clean release.
