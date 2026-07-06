import logging
import os
import sys

_CONFIGURED = False
_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

def configure_logging():
    global _CONFIGURED

    raw_level = os.getenv("LOG_LEVEL", "INFO").upper()
    level = _LEVELS.get(raw_level, logging.INFO)

    if _CONFIGURED:
        logging.getLogger().setLevel(level)
        return

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    logging.captureWarnings(True)
    _CONFIGURED = True

    if raw_level not in _LEVELS:
        logging.getLogger("agent.logging").warning(
            "Invalid LOG_LEVEL=%r; using INFO", raw_level
        )

def get_logger(name):
    configure_logging()
    if name == "__main__":
        name = "agent"
    elif not name.startswith("agent"):
        name = f"agent.{name}"
    return logging.getLogger(name)