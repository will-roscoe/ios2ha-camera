"""Sphinx configuration for the ios2ha-camera documentation.

The prose that already exists lives in Markdown at the repository root
(README, ARCHITECTURE, CONFIGURATION, OPERATIONS) because that is where it is
useful to someone browsing the repository on GitHub. MyST pulls those files in
rather than copying them, so there is exactly one copy of every sentence.
"""

import os
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

sys.path.insert(0, os.path.abspath("../src"))

# -- Project information ------------------------------------------------------
project = "ios2ha-camera"
author = "Will Roscoe"
copyright = "2026, Will Roscoe"

try:
    release = _pkg_version("ios2ha-camera")
except PackageNotFoundError:
    # Building from a source checkout without an install. The docs still build;
    # only the version in the sidebar is unknown.
    release = "0.0.0+unknown"
version = release

# -- General configuration ----------------------------------------------------
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.intersphinx",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx_autodoc_typehints",
    "sphinx_copybutton",
    "myst_parser",
]

# `colon_fence` allows ::: blocks, which survive being read as plain Markdown on
# GitHub; `linkify` turns bare URLs into links.
myst_enable_extensions = ["colon_fence", "deflist", "fieldlist", "substitution"]
myst_heading_anchors = 3

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "numpy": ("https://numpy.org/doc/stable/", None),
}

# The phone and the broker are not importable in a docs build.
autodoc_mock_imports = ["cv2", "paho", "pymobiledevice3"]
autodoc_default_options = {"members": True, "undoc-members": True, "show-inheritance": True}
autodoc_typehints = "description"

nitpicky = False

# The root Markdown files link to each other relatively ("see OPERATIONS.md"),
# which is correct on GitHub -- the surface most people read them on -- but does
# not resolve once MyST has included them at docs/supervisor/*. Rewriting those
# links to Sphinx references would break the GitHub view to fix the smaller one.
# The warning is suppressed rather than the links changed; CI still builds with
# -W, so every other warning class remains fatal.
#
# This goes away if the prose is ever moved into docs/ proper rather than
# included from the root.
suppress_warnings = ["myst.xref_missing"]

# -- HTML ---------------------------------------------------------------------
html_theme = "furo"
html_static_path = ["_static"]
html_title = f"ios2ha-camera {release}"

extlinks_detect_hardcoded_links = True
