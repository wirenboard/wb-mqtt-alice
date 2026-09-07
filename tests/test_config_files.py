"""Tests for preserving and atomically saving Alice user configurations."""

import json
from pathlib import Path

import pytest
from wb.mqtt_alice.config import main as configurator


@pytest.mark.parametrize("loader_name", ["load_config", "load_client_config"])
def test_invalid_configuration_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    loader_name: str,
) -> None:
    """
    A malformed user file must remain available for the user to repair.
    """
    config_path = tmp_path / "alice.conf"
    invalid_content = "{broken"
    config_path.write_text(invalid_content, encoding="utf-8")
    constant_name = "DEVICES_CONFIG_PATH" if loader_name == "load_config" else "CLIENT_CONFIG_PATH"
    replacement = config_path if loader_name == "load_config" else str(config_path)
    monkeypatch.setattr(configurator, constant_name, replacement)

    with pytest.raises(json.JSONDecodeError):
        getattr(configurator, loader_name)()

    assert config_path.read_text(encoding="utf-8") == invalid_content


def test_missing_client_configuration_is_not_recreated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The packaged default must not silently replace a missing user configuration.
    """
    config_path = tmp_path / "missing.conf"
    monkeypatch.setattr(configurator, "CLIENT_CONFIG_PATH", str(config_path))

    with pytest.raises(FileNotFoundError):
        configurator.load_client_config()

    assert not config_path.exists()


def test_saving_devices_configuration_is_atomic_and_preserves_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Saving replaces the complete JSON file and keeps its access mode.
    """
    config_path = tmp_path / "devices.conf"
    config_path.write_text("{}", encoding="utf-8")
    config_path.chmod(0o640)
    monkeypatch.setattr(configurator, "DEVICES_CONFIG_PATH", config_path)
    config = configurator.Config(rooms={}, devices={})

    configurator.save_devices_config(config)

    assert json.loads(config_path.read_text(encoding="utf-8")) == {"rooms": {}, "devices": {}}
    assert config_path.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob(".tmp_config_*.json"))
