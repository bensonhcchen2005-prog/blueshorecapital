"""
Structured logging setup.
JSON logs to file, human-readable logs to console.
"""

import logging
import logging.handlers
import json
from datetime import datetime
from pathlib import Path


class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "module": record.module,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry)


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    """Configure logging for the trading system."""
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Console handler — human readable
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)-20s | %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(console)

    # File handler — JSON structured
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "system.log",
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JSONFormatter())
    root.addHandler(file_handler)

    # Suppress noisy third-party loggers
    logging.getLogger("moomoo").setLevel(logging.WARNING)
