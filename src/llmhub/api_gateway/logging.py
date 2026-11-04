"""Log hygiene: secrets are masked, request bodies are never logged."""

from __future__ import annotations

import logging
import re

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


def mask(text: str) -> str:
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


class SecretMaskFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = str(record.msg)
        record.msg = mask(message)
        record.args = None
        return True


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    flt = SecretMaskFilter()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        if not any(isinstance(f, SecretMaskFilter) for f in handler.filters):
            handler.addFilter(flt)
