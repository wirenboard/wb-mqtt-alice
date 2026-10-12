"""
Single place that knows where the package version comes from.

At runtime the version is read from the installed package metadata. That metadata is filled by
setup.py at build time, which is the only moment when debian/changelog is available.
"""

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

    Everything after ~ is the suffix CI adds on dev branches, and PEP 440 allows no ~ in a
    version, so it is dropped.

    Examples:
        >>> parse_changelog_version("wb-mqtt-alice (1.0.0) stable; urgency=medium")
        '1.0.0'
        >>> parse_changelog_version("wb-mqtt-alice (0.13.6~exp~PR+73~10~g8b02ece) stable; urgency=medium")
        '0.13.6'
    """
    return changelog_line.split()[1][1:-1].split("~")[0]


def get_version_from_changelog() -> str:
    """
    Version for the packaging metadata. Build time only, called by setup.py.
    """
    with open(CHANGELOG_FILEPATH, "r", encoding="utf-8") as f:
        return parse_changelog_version(f.readline())
