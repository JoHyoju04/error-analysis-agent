"""Logging configured once, with a filter that keeps secrets out of the log."""

from __future__ import annotations

import logging
import sys

from .redaction import redact


class RedactingFilter(logging.Filter):
    """Redacts the formatted message of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # The Slack SDK logs full event payloads at DEBUG, which would include the
    # Sentry alert body; keep it at INFO regardless of our level.
    for noisy in ("slack_bolt", "slack_sdk", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(logging.INFO, root.level))
