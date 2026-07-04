"""Structured logging with PII redaction — the observability foundation.

Every log line is JSON, carries whatever context has been bound
(tenant_id, lead_id, run_id), and passes through a redaction processor
before rendering:

- any string that looks like an email address is replaced by
  ``<email:{sha256-prefix}>`` — the same digest used by suppression, so a
  redacted line remains correlatable to a suppression entry;
- values under secret-ish or person-identifying keys are dropped outright.

Redaction happens in-process, before the line exists anywhere — not as a
post-hoc scrub.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

from relay.hashing import hash_email

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# Keys whose values are never logged, whatever they contain.
_DENY_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "authorization",
    "master_key",
    "raw_email",
    "email",
    "recipient_email",
    "first_name",
    "last_name",
    "full_name",
    "subject",
    "body",
}

_REDACTED = "[REDACTED]"


def _redact_string(value: str) -> str:
    return _EMAIL_RE.sub(lambda m: f"<email:{hash_email(m.group(0))[:12]}>", value)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return {k: _redact_item(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(v) for v in value]
    return value


def _redact_item(key: Any, value: Any) -> Any:
    if isinstance(key, str) and key.lower() in _DENY_KEYS:
        return _REDACTED
    return _redact_value(value)


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Redact an arbitrary dict (also used for audit-log payloads)."""
    return {k: _redact_item(k, v) for k, v in payload.items()}


def redact_pii_processor(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    return {k: _redact_item(k, v) for k, v in event_dict.items()}


class _StderrLogger:
    """Writes each rendered line to the *current* sys.stderr.

    Resolved per call (not cached at configure time) so test harnesses
    that swap the stream still capture every line — the redaction tests
    depend on seeing real output.
    """

    def msg(self, message: str) -> None:
        print(message, file=sys.stderr)

    log = debug = info = warning = error = critical = fatal = msg
    exception = msg


class _StderrLoggerFactory:
    def __call__(self, *args: Any) -> _StderrLogger:
        return _StderrLogger()


def setup_logging(level: int = logging.INFO) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_pii_processor,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=_StderrLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def bind_run_context(**kwargs: Any) -> None:
    """Bind tenant_id / lead_id / run_id etc. onto every subsequent line."""
    structlog.contextvars.bind_contextvars(
        **{k: str(v) for k, v in kwargs.items() if v is not None}
    )


def clear_run_context() -> None:
    structlog.contextvars.clear_contextvars()
