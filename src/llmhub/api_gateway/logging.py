"""Log hygiene: secrets are masked, request bodies are never logged."""

from __future__ import annotations

import logging
import re

from llmhub.model_router.secrets import PATTERNS as SECRET_PATTERNS

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"), r"\1 ***"),
    (re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?)[^\s'\",}]+"), r"\1***"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password)(['\"]?\s*[:=]\s*['\"]?)[^\s'\",&}]+"
        ),
        r"\1\2***",
    ),
    (re.compile(r"\blh_[A-Za-z0-9_-]{8,}"), "lh_***"),
]


# the request secret detector's patterns, matched case-insensitively on the original text
_PATTERNS += [
    (re.compile(p.rx.pattern, p.rx.flags | (re.IGNORECASE if p.fold else 0)), "***")
    for p in SECRET_PATTERNS
]


def mask(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


class SecretMaskFilter(logging.Filter):
    """Masks the message, the traceback text and the stack text of a record. The traceback is
    rendered here (masked) into `exc_text` and `exc_info` is dropped, so no handler formats the
    raw exception text later."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Never raises (R3-4): a logging call must not crash a supervised loop. A record
        that cannot be masked is replaced by a placeholder rather than logged raw."""
        try:
            self._mask(record)
        except Exception:  # noqa: BLE001 - the filter must never propagate
            record.msg = "<log record dropped by mask filter>"
            record.args = None
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True

    @staticmethod
    def _mask(record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - bad format/args (KeyError, TypeError, ...)
            message = str(record.msg)
        record.msg = mask(message)
        record.args = None
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = logging.Formatter().formatException(record.exc_info)
            record.exc_info = None
        if record.exc_text:
            record.exc_text = mask(record.exc_text)
        if record.stack_info:
            record.stack_info = mask(record.stack_info)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    flt = SecretMaskFilter()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        if not any(isinstance(f, SecretMaskFilter) for f in handler.filters):
            handler.addFilter(flt)
