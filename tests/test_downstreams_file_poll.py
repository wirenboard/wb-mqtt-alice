#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import json
from pathlib import Path

from wb_alice_config.downstream.file_poll.adapter import FilePollAdapter, FilePollConfig
from wb_alice_config.downstream.models import RawDownstreamMessage


def test_file_poll_adapter_reads_json_file(tmp_path: Path):
    """
    This test checks the simplest possible "downstream adapter" contract:

    - We have a JSON file (acts like a fake data source)
    - The adapter must read it and emit one message per key/value via the handler callback
    """
    p = tmp_path / "data.json"
    p.write_text(json.dumps({"t": 23.5, "relay": 1}), encoding="utf-8")

    cfg = FilePollConfig(path=str(p))
    adapter = FilePollAdapter(cfg=cfg)

    got = []

    def on_msg(msg: RawDownstreamMessage) -> None:
        got.append(msg)

    adapter.start(on_msg)

    assert len(got) == 2
    
    addresses = set()
    for m in got:
        addresses.add(m.address)

    assert addresses == {"t", "relay"}
