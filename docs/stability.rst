Stability and pre-releases
==========================

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
``pre`` or ``prepre``. When several directives land in the same batch of commits,
the highest-impact one wins: ``major`` > ``minor`` > ``patch`` > ``rc`` > ``pre`` >
``prepre``.

The pre-release ladder
-----------------------

Pre-releases follow SemVer 2.0.0 precedence, with three channels ordered
``alpha < beta < rc``. They are reached with the same ``+:<spec>`` directives as
any other bump, using ``pre``, ``prepre`` or ``rc``.

From a final release ``X.Y.Z``:

* ``+:pre`` → ``X.(Y+1).0-alpha``
* ``+:prepre`` → ``X.(Y+1).0-alpha.0``
* ``+:rc`` → ``X.(Y+1).0-rc``

A pre-release always targets the **next** minor, never the current one, so that
it sorts after the base version that is already released. There is no directive
for a pre-release of a major; hand-tag one instead.

Within an existing pre-release ``B-channel[.n]``:

* ``+:prepre`` increments the number: ``-alpha`` → ``-alpha.1`` → ``-alpha.2``.
  A bare channel with no number counts as an implicit ``0``.
* ``+:pre`` advances the channel: ``alpha`` → ``beta`` → ``rc`` → the final
  ``B``.
* ``+:rc`` jumps any earlier channel straight to ``B-rc``, or increments it if
  already there: ``-rc`` → ``-rc.1``.
* ``+:patch`` / ``+:minor`` finalise to ``B`` immediately — the base already
  carries the bump.
* ``+:major`` finalises to ``B`` if ``B`` is already an ``X.0.0``, otherwise
  bumps to ``(X+1).0.0``.

A plain Conventional Commit with no directive, landing while a pre-release is
in progress, only advances the pre-release number. It never changes channel and
never escapes to a final release by itself — leaving the channel always needs a
directive.

Worked example, starting from a released ``2.6.0``:

.. list-table::
   :header-rows: 1
   :widths: 20 18 20 42

   * - Starting version
     - Commit carries
     - Result
     - Why
   * - ``2.6.0``
     - ``+:pre``
     - ``2.7.0-alpha``
     - Targets the next minor, bare channel.
   * - ``2.7.0-alpha``
     - ``+:prepre``
     - ``2.7.0-alpha.1``
     - A bare channel counts as an implicit ``0``.
   * - ``2.7.0-alpha.1``
     - ``fix: ...`` (no directive)
     - ``2.7.0-alpha.2``
     - A plain commit during a pre-release only advances the number.
   * - ``2.7.0-alpha.2``
     - ``+:pre``
     - ``2.7.0-beta``
     - Advances the channel; the number resets on the fresh channel.
   * - ``2.7.0-beta``
     - ``+:rc``
     - ``2.7.0-rc``
     - Jumps straight to ``rc`` from any earlier channel.
   * - ``2.7.0-rc``
     - ``+:rc``
     - ``2.7.0-rc.1``
     - Already at ``rc``, so this increments instead of jumping.
   * - ``2.7.0-rc.1``
     - ``+:pre``
     - ``2.7.0``
     - Past ``rc``, ``pre`` finalises to the plain base version.
   * - ``2.7.0-alpha`` (alt.)
     - ``+:major``
     - ``3.0.0``
     - The base ``2.7.0`` is not already an ``X.0.0``, so ``major`` bumps to the
       next one.

.. warning::

   Pre-releases cannot actually be cut in this repository today. ``+:pre`` /
   ``+:prepre`` / ``+:rc`` compute a correct pre-release string, but two things
   stop it from ever becoming a tag:

   * ``tests/test_version.py``'s ``test_version_is_semver`` requires
     ``pyproject.toml``'s version to match ``\d+\.\d+\.\d+`` exactly — no
     suffix is accepted. Bumping ``pyproject.toml`` to a pre-release string such
     as ``2.7.0-alpha`` fails that test outright, and the test suite gates every
     release.
   * Even set that test aside, the release compares the version literally: it
     requires ``pyproject.toml`` to already equal the computed version character
     for character, and cannot pass a pre-release string while that value is
     constrained to a plain ``X.Y.Z``.

   So until ``test_version_is_semver`` is loosened to accept a pre-release
   suffix, a ``+:pre`` / ``+:prepre`` / ``+:rc`` directive is a correct
   computation with nowhere to land — it will never produce a tag or a
   published Release.

How a release happens
----------------------

Release automation is switched on. The ``ghtools`` workflow
(``.github/workflows/ghtools.yml``, which runs the shared
`ghtools <https://github.com/will-roscoe/.ghtools>`__ pipeline configured in
``.github/ghtools.toml``) runs on every push to ``main``, which in practice
means every ``develop`` → ``main`` merge:

#. It runs the full CI suite as a gate — lint, both supported Python versions,
   the container image build and the docs build — against the exact commit
   being considered. Nothing is tagged unless all of it passes.
#. It reads every Conventional Commit message since the last ``v*`` tag and
   works out the next version (``ghtools version next``; the rules above are
   exactly what it applies).
#. If those commits release nothing — all ``chore`` / ``docs`` / ``style`` /
   ``refactor`` / ``build`` / ``ci`` / ``test``, and no override directive —
   the run ends having tagged nothing.
#. If a release is implied, it checks that ``pyproject.toml`` already declares
   that exact version. If it does, it tags ``vX.Y.Z`` and publishes a GitHub
   Release. If it does not — a forgotten bump — the run fails loudly instead of
   publishing a tag that disagrees with what the running container reports.

The workflow can also be run by hand from the Actions tab, with a ``dry-run``
input (on by default) that computes and prints the next version without
tagging anything, so a merge's effect can be previewed before it happens.
Tags pushed by hand are not released; releases come only from ``main``.

The version number itself is still a manual, human decision: it is bumped by
hand in ``pyproject.toml`` alongside a matching ``CHANGELOG.md`` heading.
``tests/test_version.py`` enforces that the two agree, so a release with a
changelog entry for the wrong version fails CI before it can be tagged.

What a release is
------------------

Nothing is pushed to a registry. The GitHub Release exists to carry release
notes and give a version a browsable home, not to ship an artifact — a
deployment builds its own image from a checkout, on the target host, via
``deploy.sh``. Tagging a version and deploying it are two separate steps, and
the second is never automatic.

Marking changes in these docs
------------------------------

See :doc:`guarantees` for what a given kind of bump promises, and
:doc:`changelog` for the full prose history these rules have produced.

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
