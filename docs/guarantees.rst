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

Not covered by this promise
---------------------------

.. note::

   Scaffolded. This section needs to state plainly what callers must *not* rely on,
   so that changing it later is not a broken promise. At minimum: the Python API in
   ``ios2ha_camera``, the log format, the on-disk archive layout, and the internal
   tuning constants documented in :doc:`supervisor/configuration`.

Finding the running version
---------------------------

Three places, and they agree:

* the first line of the container log
* ``sw_version`` on the Home Assistant device page
* ``./deploy.sh version``

Each carries the git description as well as the number, so an image built from a
modified tree reads as ``1.0.0-dirty`` rather than passing as a clean release.
