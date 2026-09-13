ios2ha-camera
=============

A supervisor that turns an old iPhone running a camera app into a Home Assistant
camera: it holds the connection to the phone only while something needs it, stacks
frames to see in the dark, and publishes the result over MQTT.

.. note::

   **Two different things are called "supervisor" here.** Home Assistant has a
   component of its own by that name, which manages add-ons and the operating
   system. It has nothing to do with this project. Throughout these docs,
   *the supervisor* means the service in this repository, and Home Assistant's is
   always written out as *Home Assistant Supervisor*. The two sections below keep
   the same split: :doc:`supervisor/index` is our service, and
   :doc:`home-assistant/index` is everything on the Home Assistant side of the
   boundary.

.. toctree::
   :maxdepth: 2
   :caption: The supervisor

   supervisor/index

.. toctree::
   :maxdepth: 2
   :caption: Home Assistant

   home-assistant/index

.. toctree::
   :maxdepth: 1
   :caption: Promises

   guarantees
   stability
   upgrading

.. toctree::
   :maxdepth: 1
   :caption: Reference

   api/index
   changelog
