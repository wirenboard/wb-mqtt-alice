#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from typing import Any, Mapping, Optional

from ..base import DownstreamCodec
from ...models import PointSpec

class FilePollCodec(DownstreamCodec):
    """
    Identity codec for file_poll

    Passthrough codec - we only do minimal normalization if needed
    The JSON file already contains python-native types (bool/int/float/str)
    """

    @property
    def downstream_name(self) -> str:
        return "file_poll"

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
