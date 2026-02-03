# upstream/types.py

from enum import Enum

class UpstreamState(Enum):
    # Adapter created but start() not called yet or not complete
    INITIALIZING = "initializing"

    # Fully operational
    # Example: сonnected to cloud OR Server running
    READY = "ready"

    # Adapter is still running but cannot transmit data
    # Example: temporary connectivity loss or error state
    #          Disconnected / Error / Reconnecting
    UNAVAILABLE = "unavailable"

    # Explicitly stopped via stop()
    STOPPED = "stopped"
