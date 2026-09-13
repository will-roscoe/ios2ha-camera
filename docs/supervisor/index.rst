The supervisor
==============

The service in this repository. It is the sole client of the phone: it owns the
connection, decides when to hold it open, stacks frames into a still, and reports
everything it knows over MQTT.

(This is *not* Home Assistant Supervisor, which is an unrelated component of Home
Assistant that manages add-ons and the host OS.)

.. toctree::
   :maxdepth: 2

   overview
   installation
   architecture
   configuration
   operations
