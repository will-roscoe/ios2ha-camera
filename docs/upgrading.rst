Upgrading
=========

.. note::

   Scaffolded. Each entry should say what to do, not only what changed, and only
   versions that need an action belong here. :doc:`changelog` carries the full
   history.

Upgrading in general
--------------------

.. code-block:: console

   $ ./deploy.sh
   $ ./deploy.sh version    # confirm what is actually running

Downgrading below 1.1.0
-----------------------

``stream_state`` gained a new option in 1.1.0. A downgrade leaves the retained
discovery message advertising an option the older code never publishes, and Home
Assistant rejects a state that is not in the option list.

The next tick republishes the real state within 30 seconds and clears this on its
own. To fix it immediately, clear or republish the retained
``ios2ha_camera/state/stream_state`` topic.
