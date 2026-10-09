"""
Single place that knows where the package version comes from.

At runtime the version is read from the installed package metadata. That metadata is filled by
setup.py at build time, which is the only moment when debian/changelog is available.
"""

import os
from importlib.metadata import version

# Relative to the source root, which is the working directory setup.py is always run from.
CHANGELOG_FILEPATH = "debian/changelog"

# Spelled out because this module lives in a subpackage: __package__ would resolve to
# "wb-mqtt-alice-common", which is not a distribution.
DIST_NAME = "wb-mqtt-alice"


def get_version() -> str:
    """
    Version of the installed package. Use this at runtime.
    """
    return version(DIST_NAME)


def parse_changelog_version(changelog_line: str) -> str:
    """
    Pull the version out of the first line of debian/changelog.

    Examples:
        >>> parse_changelog_version("wb-mqtt-alice (1.0.0) stable; urgency=medium")
        '1.0.0'
        >>> parse_changelog_version("wb-mqtt-alice (0.13.9~exp~PR+77~2~g63a9abd) stable; urgency=medium")
        '0.13.9'
    """
    return changelog_line.split()[1][1:-1].split("~")[0].replace("-", "+")


def get_version_from_changelog() -> str:
    """
    Version for the packaging metadata. Build time only, called by setup.py.
    """
    return os.environ.get("DEB_VERSION", "0.0.0")
