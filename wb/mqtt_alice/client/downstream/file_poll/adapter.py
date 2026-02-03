#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from ..base import DownstreamAdapter, RawMessageHandler
from ..models import DownstreamWrite, RawDownstreamMessage


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FilePollConfig:
    path: str
    keys: Optional[Iterable[str]] = None


class FilePollAdapter(DownstreamAdapter):
    """
    Reads a JSON file once at start() and emits messages for configured keys.

    This is intentionally minimal and synchronous.
    """

    def __init__(self, *, cfg: FilePollConfig) -> None:
        self._cfg = cfg
        self._handler: Optional[RawMessageHandler] = None

    @property
    def downstream_name(self) -> str:
        return "file_poll"

    def start(self, handler: RawMessageHandler) -> None:
        self._handler = handler
        p = Path(self._cfg.path)
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("file_poll expects JSON object (dict)")

        keys = list(self._cfg.keys) if self._cfg.keys else list(data.keys())
        for k in keys:
            if k not in data:
                continue
            handler(
                RawDownstreamMessage(
                    downstream_name=self.downstream_name,
                    address=str(k),
                    payload=data[k],
                    meta=None,
                )
            )

    def stop(self) -> None:
        return

    def write(self, req: DownstreamWrite) -> None:
        raise NotImplementedError("file_poll is read-only in this minimal implementation")
