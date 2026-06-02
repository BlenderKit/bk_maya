"""Logging configuration for the BlenderKit Maya plugin.

Mirrors the Blender addon's ``log.py`` but without any ``bpy`` dependency.

Usage
-----
Call ``configure_loggers()`` once at plugin startup (in ``maya_plugin.py``).
All other modules just use::

    import logging
    log = logging.getLogger(__name__)

That automatically routes through the ``bk_maya`` hierarchy which this module
configures.
"""

from __future__ import annotations

import logging
import re
import sys

from . import global_vars

bk_logger = logging.getLogger(__name__)


# ── Formatters ────────────────────────────────────────────────────────────────


class BlenderKitFormatter(logging.Formatter):
    """Prefix log records with an emoji for the log level and mask API keys.

    Temporary tokens (30 chars) → ``***``
    Permanent tokens (40 chars)  → ``*****``
    """

    EMOJIS = {  # noqa: RUF012
        logging.DEBUG: "🐞 ",
        logging.INFO: "ℹ️  ",
        logging.WARNING: "⚠️  ",
        logging.ERROR: "❌ ",
        logging.CRITICAL: "🔥 ",
    }

    def format(self, record: logging.LogRecord) -> str:
        record.levelname = self.EMOJIS.get(record.levelno, "")
        msg = super().format(record)
        msg = re.sub(r'(?<=["\'\s])\b[A-Za-z0-9]{30}\b(?=["\'\s])', "***", msg)
        msg = re.sub(r'(?<=["\'\s])\b[A-Za-z0-9]{40}\b(?=["\'\s])', "*****", msg)
        return msg


class SensitiveFormatter(logging.Formatter):
    """Mask API key tokens without emoji prefix (used for third-party loggers)."""

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        msg = re.sub(r'(?<=["\'\s])\b[A-Za-z0-9]{30}\b(?=["\'\s])', "***", msg)
        msg = re.sub(r'(?<=["\'\s])\b[A-Za-z0-9]{40}\b(?=["\'\s])', "*****", msg)
        return msg


def _bk_formatter() -> BlenderKitFormatter:
    return BlenderKitFormatter(
        fmt="%(levelname)sbk_maya: %(message)s [%(asctime)s.%(msecs)03d, %(filename)s:%(lineno)d]",
        datefmt="%H:%M:%S",
    )


def _sensitive_formatter() -> SensitiveFormatter:
    return SensitiveFormatter(
        fmt="bk_maya %(levelname)s: %(message)s [%(asctime)s.%(msecs)03d, %(filename)s:%(lineno)d]",
        datefmt="%H:%M:%S",
    )


# ── Configuration helpers ─────────────────────────────────────────────────────


def configure_bk_logger() -> None:
    """Configure the root ``bk_maya`` logger.

    All ``logging.getLogger(__name__)`` calls inside ``bk_maya.*`` submodules
    propagate to this logger automatically.
    """
    logger = logging.getLogger("bk_maya")
    logger.setLevel(global_vars.LOGGING_LEVEL_BLENDERKIT)
    logger.propagate = False
    logger.handlers = []

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_bk_formatter())
    logger.addHandler(handler)


def configure_imported_loggers() -> None:
    """Silence noisy third-party loggers (urllib3, requests, …)."""
    for name in ("urllib3", "requests", "urllib3.connectionpool"):
        lib_logger = logging.getLogger(name)
        lib_logger.propagate = False
        lib_logger.handlers = []
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(global_vars.LOGGING_LEVEL_IMPORTED)
        handler.setFormatter(_sensitive_formatter())
        lib_logger.addHandler(handler)


def configure_loggers() -> None:
    """Configure all loggers.  Call once at plugin startup."""
    configure_bk_logger()
    configure_imported_loggers()
