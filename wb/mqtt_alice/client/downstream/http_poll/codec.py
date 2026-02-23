#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from typing import Any, Mapping, Optional

from ..base import DownstreamCodec
from ...models import PointSpec

class HttpPollCodec(DownstreamCodec):
    """Identity codec for http_poll."""

    @property
    def downstream_name(self) -> str:
        return "http_poll"

    def decode(
        self,
        spec: PointSpec,
        value_path: str,
        raw: Any,
    ) -> Any:
        return raw

    def encode(
        self,
        spec: PointSpec,
        value_path: str,
        canonical: Any,
    ) -> Any:
        return canonical
