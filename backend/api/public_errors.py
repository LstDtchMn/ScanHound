"""Closed public error mapping for API and WebSocket boundaries."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import uuid


@dataclass(frozen=True)
class PublicError:
    code: str
    message: str
    correlation_id: str

    def as_detail(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "correlation_id": self.correlation_id,
        }

    def notification_data(self, *, title: str, priority: str = "high") -> dict:
        return {
            "title": title,
            "body": f"{self.message} (Reference: {self.correlation_id})",
            "priority": priority,
            "code": self.code,
            "correlation_id": self.correlation_id,
        }


def capture_public_exception(
    log: logging.Logger,
    exc: BaseException,
    *,
    code: str,
    message: str,
    context: str,
) -> PublicError:
    """Log full internal detail and return a closed client-safe error."""
    correlation_id = uuid.uuid4().hex[:16]
    log.error(
        "%s [code=%s correlation_id=%s]: %s",
        context,
        code,
        correlation_id,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return PublicError(code=code, message=message, correlation_id=correlation_id)
