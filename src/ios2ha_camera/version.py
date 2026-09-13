"""What is actually running.

The version comes from the installed package metadata, so `pyproject.toml` is the
single source of truth and cannot drift from a second copy in the source.

The commit and build date are baked into the image at build time, because the
deployment is an rsync of a working tree: a semantic version alone cannot tell
you whether what is running is a clean release or a release plus three
uncommitted edits. `deploy.sh` passes them; a source run reports "source".
"""

import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    VERSION = _pkg_version("ios2ha-camera")
except PackageNotFoundError:  # running from a source tree that was never installed
    VERSION = "0+unknown"

# Set by the Dockerfile from build args. `git describe --always --dirty --tags`,
# so a deployment built from a modified tree is visibly marked.
COMMIT = os.environ.get("GIT_DESCRIBE") or "source"
BUILT = os.environ.get("BUILD_DATE") or "source"


def full():
    """Version and provenance in one string, for logs and `sw_version`."""
    return f"{VERSION} ({COMMIT})"


def info():
    return {"version": VERSION, "commit": COMMIT, "built": BUILT}
