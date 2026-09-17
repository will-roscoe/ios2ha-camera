Upgrading
=========

Only versions that need you to actually do something are listed below; each
entry says what to do. :doc:`changelog` carries the full history, including
every release that needs nothing.

Upgrading in general
---------------------

.. code-block:: console

   $ ./deploy.sh
   $ ./deploy.sh version    # confirm what is actually running

Versions that need action
--------------------------

Newest first.

2.6.1
~~~~~

.. versionchanged:: 2.6.1

   The default for ``MQTT_HOST`` changed from a specific address to
   ``localhost``. If your install set neither ``MQTT_URL`` nor ``MQTT_HOST``
   and relied on the previous default, it must now set ``MQTT_URL``
   (preferred) or ``MQTT_HOST`` explicitly, e.g.::

      MQTT_URL=mqtt://<your broker host>

   Otherwise, after upgrading, the ios2ha-camera service looks for a broker on
   its own host instead of your actual broker.

2.6.0
~~~~~

.. versionchanged:: 2.6.0

   A JSON API under ``/api/v1`` is now served alongside MQTT, on by default
   (``EXPOSE_API=true``), and it has **no authentication**: anything that can
   reach the port can read every value and write every control. If the host
   is reachable from beyond a trusted network, set ``EXPOSE_API=false`` until
   authentication exists, or make sure that network is one you trust — the
   same posture already required of the MQTT broker.

   This release also corrected the shutdown-time arithmetic: the worst case
   is a full 20 s, not the 17 s previously assumed. Check your compose file's
   ``stop_grace_period`` is at least 20 s; 30 s is now recommended for margin.
   See "The compose service" in :doc:`supervisor/operations`.

2.5.0
~~~~~

.. versionchanged:: 2.5.0

   The emulated Bluetooth keyboard's GATT handles are now pinned to a fixed
   address instead of being reassigned by BlueZ on every container start.
   iOS caches the handles of a bonded keyboard and never re-reads them on a
   plain reconnect, so a pairing made before this release keeps using
   whatever handle it last discovered, and can keep silently doing nothing
   after a restart exactly as before. On the phone, forget the pairing
   (Settings → Bluetooth → the paired keyboard → Forget This Device) and pair
   it again once, so it discovers the new fixed handles.

2.4.0
~~~~~

.. versionchanged:: 2.4.0

   The service can now start streaming on the phone itself, by emulating a
   Bluetooth keyboard. Two one-time steps are needed to use it:

   #. On the host, run ``scripts/install-bluez-proxy.sh`` (as root). It
      installs a systemd unit that gives the container a D-Bus socket
      filtered to ``org.bluez`` only, which the keyboard needs and did not
      require before this release.
   #. On the phone: no passcode lock, Bluetooth on, Full Keyboard Access on,
      then pair the emulated keyboard once. It appears in the phone's
      Bluetooth device list under the host's own name, not the name it
      advertises. See :doc:`supervisor/operations` for the full walkthrough.

2.3.0
~~~~~

.. versionchanged:: 2.3.0

   ``stream_state`` gained a new value, ``stalled``, for an app whose picture
   has frozen while it still answers. This is additive and nothing already
   working breaks on its own, but if you have a custom offline-alert
   automation or template that matches ``stream_state`` by name, add a
   ``stalled`` branch to it — see the updated template in :doc:`supervisor/operations`.
   Without it, a stalled camera reads as healthy to anything that only checks
   for the values that existed before this release.

2.2.0
~~~~~

.. versionchanged:: 2.2.0

   ``MQTT_URL`` was introduced as the one setting for the broker (scheme,
   host, port, TLS and transport together). ``MQTT_HOST`` and ``MQTT_PORT``
   still work exactly as before when ``MQTT_URL`` is unset, so nothing breaks
   and no action is required. Migrating is recommended anyway: it collapses
   several variables into one, and setting both together only produces a
   startup warning naming what was ignored, rather than combining the two.

2.0.0
~~~~~

.. versionchanged:: 2.0.0

   ``stream_state``'s values changed shape, which is why this is a major
   release. Any automation or template that matches a value by name needs
   editing:

   .. list-table::
      :header-rows: 1
      :widths: 20 30 50

      * - Was
        - Now
        - Why
      * - ``unreachable``
        - ``unavailable``
        - Nothing answered: off the network, or the connection timed out.
      * - ``unreachable``
        - ``backgrounded``
        - The port refused, so the phone answered but the app is not
          listening.
      * - ``idle``
        - ``ready``
        - Reachable and willing, just not connected to at the moment.
      * - ``streaming_off``
        - ``streaming_off``
        - Unchanged.
      * - ``streaming``
        - ``capturing``
        - Frames are flowing for a still.
      * - ``streaming``
        - ``in_use``
        - Frames are flowing for a viewer; wins over ``capturing`` when both
          apply.
      * - ``streaming``
        - ``streaming``
        - Frames flowing with nobody holding, which happens only when
          ``on_demand`` is off.

   The offline-alert template also needs updating: ``camera_online`` now stays
   true while ``backgrounded`` on its own (nothing was actually lost), but
   still goes false for every other non-``ready``, non-streaming state. See
   :doc:`supervisor/operations` for the current template.

1.1.2
~~~~~

.. versionchanged:: 1.1.2

   Recommended, not required: if the cable to the phone goes through
   ``usbmuxd`` restarting (it exits whenever the phone is unplugged), the
   container's own fallback is deliberately slow: it restarts itself only
   after the fault has persisted for five minutes, and only once it has been
   up for ten, so that a cable already broken at boot cannot restart it
   forever. For a near-instant recovery instead, install
   ``scripts/install-usbmuxd-hook.sh`` on the host: it restarts the container
   within seconds of ``usbmuxd`` coming back, and is idempotent to re-run.

Downgrading below 1.1.0
------------------------

``stream_state`` gained a new option in 1.1.0. A downgrade leaves the retained
discovery message advertising an option the older code never publishes, and Home
Assistant rejects a state that is not in the option list.

The next tick republishes the real state within 30 seconds and clears this on its
own. To fix it immediately, clear or republish the retained
``ios2ha_camera/state/stream_state`` topic.

Rolling back further, or to a specific tag rather than just the previous state,
is covered in "Rolling back" in :doc:`supervisor/operations`, including the
extra step a downgrade across 2.0.0 needs.
