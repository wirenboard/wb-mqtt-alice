import logging
import json
from pathlib import Path
from constants import SERVER_CONFIG_PATH, BOARD_REVISION_PATH, BOARD_MODEL_PATH
import re

BOARD_REVISION_PATH = Path(BOARD_REVISION_PATH)
BOARD_MODEL_PATH = Path(BOARD_MODEL_PATH)
SERVER_CONFIG_PATH = Path(SERVER_CONFIG_PATH)

logger = logging.getLogger(__name__)

def load_server_config():
    """Load server configuration file"""

    logger.debug("Reading server configuration file...")
    try:
        server_config = json.loads(SERVER_CONFIG_PATH.read_text(encoding="utf-8"))
        return server_config
    except Exception as e:
        logger.error("Error reading server configuration file: %r", e)
        raise



def get_board_revision():
    """Read the controller hardware revision from Device Tree."""

    logger.debug("Reading controller hardware revision...")
    try:
        board_revision = ".".join(BOARD_REVISION_PATH.read_text().rstrip("\x00").split(".")[:2])
        logger.debug("Сontroller hardware revision: %r", board_revision)
        return board_revision
    except FileNotFoundError:
        try:
            content = BOARD_MODEL_PATH.read_text().rstrip("\x00")
            board_revision = re.search(r"rev\.\s*(\d+\.\d+)", content).group(1)
            logger.debug("Сontroller revision: %r", board_revision)
            return board_revision
        except FileNotFoundError:
            logger.error(
                "Controller board revision files not found! Check paths: %r and %r",
                BOARD_REVISION_PATH,
                BOARD_MODEL_PATH,
            )
            return None

    except Exception as e:
        logger.error("Error reading controller board revision: %r", e)
        return None


def get_key_id(controller_version: str) -> str:
    """Determine the appropriate key ID based on controller version"""
    min_version = [7, 0]
    try:
        version_parts = list(map(int, controller_version.split(".")[:2]))
        return "ATECCx08:00:02:C0:00" if version_parts >= min_version else "ATECCx08:00:04:C0:00"
    except (ValueError, AttributeError) as e:
        raise ValueError("Invalid controller version format: %r" % controller_version) from e
