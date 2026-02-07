#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


def _set_path(root: Dict[str, Any], path: str, value: Any) -> None:
    """
    Set nested value by a dotted path

    Path examples:
      - "value"        -> root["value"] = ...
      - "value.h"      -> root["value"]["h"] = ...
    """
    parts = path.split(".")
    cur: Dict[str, Any] = root
    for p in parts[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[parts[-1]] = value


def _get_path(root: Dict[str, Any], path: str) -> Optional[Any]:
    """
    Get nested value by a dotted path. Returns None if path missing
    """
    parts = path.split(".")
    cur: Any = root
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return None
        cur = cur[p]
    return cur


@dataclass
class StateStore:
    """In-memory canonical state store

    Stores canonical values per (device_id, point)

    Structure:
      state[device_id][point] -> dict with a "value" key (scalar or mapping)

    We keep it intentionally simple:
    - no history
    - no rate limits
    - no batching

    This is enough to:
    - answer "query" (what is the last known state?)
    - support router outbound writes while keeping store consistent
    """

    state: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)

    def set_value(self, device_id: str, point: str, value_path: str, value: Any) -> None:
        dev = self.state.setdefault(device_id, {})
        rec = dev.setdefault(point, {})
        _set_path(rec, value_path, value)

    def get_value(self, device_id: str, point: str, value_path: str = "value") -> Optional[Any]:
        dev = self.state.get(device_id, {})
        rec = dev.get(point)
        if rec is None:
            return None
        return _get_path(rec, value_path)
