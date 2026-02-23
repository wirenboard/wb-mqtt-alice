#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable

from .models import DownstreamWrite, RawDownstreamMessage
from ..models import PointSpec

RawMessageHandler = Callable[[RawDownstreamMessage], None]

class DownstreamAdapter(ABC):
    """
    Base interface for a downstream adapter

    Adapter responsibilities:
      - start(handler): adapter begins receiving messages from a transport
          (MQTT/HTTP/etc) and producing RawDownstreamMessage to handler
      - write(req): write raw messages to transport (publish/post/etc), router
        sends raw payload to a downstream address
      - stop(): optional, for cleanup
    """

    @property
    @abstractmethod
    def downstream_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def start(self, handler: RawMessageHandler) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def write(self, req: DownstreamWrite) -> None:
        raise NotImplementedError


class DownstreamCodec(ABC):
    """
    Base interface for raw <-> canonical conversion per downstream

    decode(): inbound raw -> canonical
    encode(): outbound canonical -> raw
    
    Notes:
      - codec should be reusable for any route/adapter instance
      - binding_meta is optional and can carry adapter-specific hints if needed
      - Router provides PointSpec so codec can interpret raw values correctly
    """

    @property
    @abstractmethod
    def downstream_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def decode(
        self,
        spec: PointSpec,
        value_path: str,
        raw: Any,
    ) -> Any:
        """
        Convert raw downstream payload into canonical value
        """
        raise NotImplementedError

    @abstractmethod
    def encode(
        self,
        spec: PointSpec,
        value_path: str,
        canonical: Any,
    ) -> Any:
        """
        Convert canonical value into raw downstream payload
        """
        raise NotImplementedError
