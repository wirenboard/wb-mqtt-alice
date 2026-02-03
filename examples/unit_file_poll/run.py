#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from wb_alice_config.downstream.file_poll.adapter import FilePollAdapter, FilePollConfig
from wb_alice_config.downstream.models import RawDownstreamMessage


@dataclass
class _RuntimeState:
    stop: bool = False
    last_payload_by_address: Optional[Dict[str, Any]] = None


def _utc_ts() -> str:
    # Human-friendly timestamp for console output
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


def _print_msg(msg: RawDownstreamMessage) -> None:
    # Single-line, stable format for tail -f / journald reading
    meta_part = ""
    if msg.meta:
        meta_part = f" meta={dict(msg.meta)!r}"
    print(f"[{_utc_ts()}] downstream={msg.downstream!r} address={msg.address!r} payload={msg.payload!r}{meta_part}")


def _make_handler(state: _RuntimeState, *, only_changes: bool) -> Any:
    """
    Returns handler(msg) closure

    If only_changes=True:
      - prints first seen value per address
      - then prints only when payload changes
    """

    def handler(msg: RawDownstreamMessage) -> None:
        if not only_changes:
            _print_msg(msg)
            return

        if state.last_payload_by_address is None:
            state.last_payload_by_address = {}

        prev = state.last_payload_by_address.get(msg.address, object())
        if prev != msg.payload:
            state.last_payload_by_address[msg.address] = msg.payload
            _print_msg(msg)

    return handler


def _install_signal_handlers(state: _RuntimeState) -> None:
    def _handle_sigint(signum: int, frame: Any) -> None:  # noqa: ARG001
        state.stop = True

    # SIGINT (Ctrl+C) is enough for interactive demo
    signal.signal(signal.SIGINT, _handle_sigint)

    # Some environments send SIGTERM (systemd stop); handle it too
    try:
        signal.signal(signal.SIGTERM, _handle_sigint)
    except Exception:
        # Not all platforms allow SIGTERM handler installation (e.g. limited envs)
        pass


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="wb-alice-file-poll-demo",
        description=(
            "Demo: poll a JSON file and print RawDownstreamMessage events.\n\n"
            "The underlying FilePollAdapter reads the file once per start().\n"
            "This demo re-reads periodically by calling start() repeatedly."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    ap.add_argument(
        "--path",
        default=None,
        help="Path to JSON file. If omitted, uses demo.json next to this script.",
    )
    ap.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Polling interval in seconds (default: 1.0)",
    )
    ap.add_argument(
        "--keys",
        default="",
        help=(
            "Comma-separated keys to read (default: all keys).\n"
            "Example: --keys temperature,relay,state"
        ),
    )
    ap.add_argument(
        "--only-changes",
        action="store_true",
        help="Print only when a key value changes (recommended)",
    )

    args = ap.parse_args(argv)

    if args.interval <= 0:
        print("ERROR: --interval must be > 0", file=sys.stderr)
        return 2

    keys = None
    if str(args.keys).strip():
        keys = [k.strip() for k in str(args.keys).split(",") if k.strip()]
        if not keys:
            keys = None

    # If --path is not provided, resolve demo.json relative to this file,
    # not relative to current working directory (CWD)
    if args.path is None:
        args.path = str(Path(__file__).resolve().parent / "demo.json")

    state = _RuntimeState(stop=False, last_payload_by_address=None)
    _install_signal_handlers(state)

    handler = _make_handler(state, only_changes=bool(args.only_changes))

    print(
        f"[{_utc_ts()}] file_poll_demo started: path={args.path!r} interval={args.interval}s "
        f"keys={keys!r} only_changes={bool(args.only_changes)}",
        flush=True,
    )

    # Simple periodic loop: create adapter, call start() to emit messages, sleep, repeat
    # No queues, no threads: minimal, predictable
    while not state.stop:
        adapter = FilePollAdapter(cfg=FilePollConfig(path=str(args.path), keys=keys))
        try:
            adapter.start(handler)
        except Exception as e:
            # Keep demo running even if file is temporarily invalid / being edited
            print(f"[{_utc_ts()}] ERROR: poll failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        finally:
            try:
                adapter.stop()
            except Exception:
                # Adapter.stop() is no-op in current implementation, but keep safe
                pass

        # Sleep in small steps so Ctrl+C stops quickly even with large interval
        remaining = float(args.interval)
        step = 0.1
        while remaining > 0 and not state.stop:
            time.sleep(step if remaining > step else remaining)
            remaining -= step

    print(f"[{_utc_ts()}] file_poll_demo stopped", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
