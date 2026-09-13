Stability and pre-releases
==========================

.. note::

   Scaffolded. The release automation described here is not switched on yet: see
   :doc:`supervisor/index` and ``CONTRIBUTING.md`` for what exists today.

How versions are decided
------------------------

Releases are computed from `Conventional Commits
<https://www.conventionalcommits.org/>`_:

* ``feat:`` → minor
* ``fix:`` / ``perf:`` / ``revert:`` → patch
* a breaking change (``type!:`` or a ``BREAKING CHANGE:`` footer) → major
* ``chore`` / ``docs`` / ``style`` / ``refactor`` / ``build`` / ``ci`` / ``test`` →
  no release

Where that classification disagrees with the promise in :doc:`guarantees`, the
promise wins, and the commit says so with an override directive.

Override directives
-------------------

A ``+:<spec>`` token **alone on its own line** of a commit message overrides the
classification, where ``<spec>`` is one of ``major``, ``minor``, ``patch``, ``rc``,
``pre`` or ``prepre``.

The pre-release ladder
----------------------

.. note::

   Scaffolded. Document the alpha → beta → rc ladder, how ``pre``/``prepre``/``rc``
   move along it, and how a pre-release finalises.

Marking changes in these docs
-----------------------------

Use Sphinx's own directives so a reader can tell what is new:

.. code-block:: rst

   .. versionadded:: 1.1.0
   .. versionchanged:: 1.1.1
   .. deprecated:: 1.2.0

For example:

.. versionadded:: 1.1.0
   The on-demand upstream, the stacked live feed at ``/live/stacked``, and seven
   new entities.

.. versionchanged:: 1.1.1
   Daylight stills keep the camera's own rendering; the night render is unchanged.
