import re

import pytest

from wb.mqtt_alice.cli import main as cli_main
from wb.mqtt_alice.common.version import get_version

# WB Debian packaging expects exactly MAJOR.MINOR.PATCH. Neither dpkg nor PEP 440 enforces it,
# both accept 1.0 and 1.2.3.4, so this test is the only thing that does.
RELEASE_VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")


def _is_release_version(package_version: str) -> bool:
    return RELEASE_VERSION_PATTERN.fullmatch(package_version) is not None


def test_installed_version_is_release_version() -> None:
    package_version = get_version()
    assert _is_release_version(
        package_version
    ), f"package version '{package_version}' is not MAJOR.MINOR.PATCH, fix debian/changelog"


def test_four_part_version_is_rejected() -> None:
    assert not _is_release_version("1.0.0.1")


def test_version_argument_prints_version_and_exits(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli_main.main(["--version"])

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == get_version()
