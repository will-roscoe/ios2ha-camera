Home Assistant
==============

Everything on the Home Assistant side of the boundary: the entities the supervisor
publishes, how to connect it, and what is promised about that surface.

There are two supported ways to connect, and they are documented separately because
they need different things from you:

**MQTT and camera entities**
   Works today, with any Home Assistant install. The supervisor publishes MQTT
   discovery messages and serves MJPEG, so entities appear on their own once the
   broker credentials are set. Needs a broker, and needs the two camera entries
   adding by hand.

**The native integration**
   Not built yet. A custom component installed through HACS that talks to the
   supervisor directly, with no broker and no manual entity setup.

.. toctree::
   :maxdepth: 2

   entities
   mqtt
   integration
