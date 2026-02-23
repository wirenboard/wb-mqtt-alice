#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

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
      - read(address, timeout): one-off async read for current state queries
      - stop(): optional, for cleanup
      - pause/resume_subscriptions(): optional, for connection issues
    """

    @property
    @abstractmethod
    def downstream_name(self) -> str:
        """
        Identifier for this downstream transport
        """
        raise NotImplementedError

    @abstractmethod
    def start(self, handler: RawMessageHandler) -> None:
        """
        Start receiving messages from transport and pass them to handler
        """
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        """
        Stop the adapter and clean up connections
        """
        raise NotImplementedError

    @abstractmethod
    def write(self, req: DownstreamWrite) -> None:
        """
        Send raw payload to the downstream address
        """
        raise NotImplementedError

    @abstractmethod
    async def read(self, address: str, timeout: float = 2.0) -> Optional[RawDownstreamMessage]:
        """
        One-off read of current state (e.g. retained message)
        Used by Dispatcher to answer platform Query requests
        """
        raise NotImplementedError

    def pause_subscriptions(self) -> None:
        """
        Pause receiving events from transport
        Implementation is optional, default is no-op
        """
        pass

    def resume_subscriptions(self) -> None:
        """
        Resume receiving events from transport
        Implementation is optional, default is no-op
        """
        pass

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
        """
        Identifier for this downstream codec
        """
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
