#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

import httpx

from ..base import DownstreamAdapter, RawMessageHandler
from ..models import DownstreamWrite, RawDownstreamMessage


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HttpPollConfig:
    url: str
    keys: Optional[Iterable[str]] = None
    timeout: float = 5.0


class HttpPollAdapter(DownstreamAdapter):
    """
    One-shot HTTP poll: GET JSON and emit messages for configured keys
    """

    def __init__(self, *, cfg: HttpPollConfig, client: Optional[httpx.Client] = None) -> None:
        self._cfg = cfg
        self._client = client or httpx.Client(timeout=cfg.timeout)
        self._handler: Optional[RawMessageHandler] = None

    @property
    def downstream_name(self) -> str:
        return "http_poll"

    def start(self, handler: RawMessageHandler) -> None:
        self._handler = handler
        r = self._client.get(self._cfg.url)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError("http_poll expects JSON object (dict)")

        keys = list(self._cfg.keys) if self._cfg.keys else list(data.keys())
        for k in keys:
            if k not in data:
                continue
            handler(
                RawDownstreamMessage(
                    downstream_name=self.downstream_name,
                    address=str(k),
                    payload=data[k],
                    meta={"url": self._cfg.url},
                )
            )

    def stop(self) -> None:
        try:
            self._client.close()
        except Exception:
            logger.exception("http_poll client close failed")

    def write(self, req: DownstreamWrite) -> None:
        raise NotImplementedError("http_poll is read-only in this minimal implementation")
