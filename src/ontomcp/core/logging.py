"""Tiny logging setup shared by both servers.

Reads the level from ``ONTOMCP_LOG_LEVEL`` (default INFO) unless one is passed
explicitly. Stdlib only — no third-party deps.
"""

import logging
import os

LOGGER_NAME = "ontomcp"


def configure_logging(level: str | None = None) -> logging.Logger:
    """Configure root logging once and return the ``ontomcp`` logger.

    Precedence: explicit ``level`` arg > ``ONTOMCP_LOG_LEVEL`` env var > INFO.
    Safe to call more than once; ``basicConfig`` is a no-op after the first call.
    """
    resolved = (level or os.environ.get("ONTOMCP_LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return logging.getLogger(LOGGER_NAME)
