"""
logger_setup.py

Configures the root "affiliate_bot" logger with:
- RotatingFileHandler → logs/affiliate_bot.log (5 MB × 5 backups)
- StreamHandler       → console (INFO and above)

Usage:
    from logger_setup import setup_logger
    logger = setup_logger()          # call once in main.py
    logger.info("Bot started")

Child loggers in other agents:
    import logging
    logger = logging.getLogger("affiliate_bot.trend_agent")
"""

import logging
import os
from logging.handlers import RotatingFileHandler

_LOGGER_NAME = "affiliate_bot"
_LOG_FILE    = "affiliate_bot.log"
_MAX_BYTES   = 5 * 1024 * 1024   # 5 MB
_BACKUP_COUNT = 5
_FMT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"


def setup_logger(name: str = _LOGGER_NAME) -> logging.Logger:
    """Create (or return cached) root logger with file + console handlers.

    The logs/ directory is created automatically next to the project root
    regardless of the working directory from which the script is invoked.
    """
    logger = logging.getLogger(name)

    # Guard: skip setup if handlers already attached (e.g. called twice)
    if logger.handlers:
        return logger

    # Resolve logs/ relative to this file's grandparent (project root)
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    logs_dir = os.path.join(project_root, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, _LOG_FILE)

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(_FMT)

    # --- Rotating file handler (DEBUG+) ---
    fh = RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    # --- Console handler (INFO+) ---
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger
