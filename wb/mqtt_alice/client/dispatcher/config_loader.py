#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class LoadedConfig:
    """
    Thin wrapper over loaded JSON.

    Example (output):
        LoadedConfig(raw={...full config dict...})
    """
    raw: Dict[str, Any]


def load_config(path: str) -> LoadedConfig:
    """
    Load JSON config from disk.

    Input:
      path: path to JSON file.

    Output:
      LoadedConfig with .raw containing parsed dict.

    Example:
      cfg = load_config("config_example.json").raw
      devices = cfg["devices"]
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return LoadedConfig(raw=data)
