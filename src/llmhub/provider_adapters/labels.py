"""HMAC resource labels: `llmhub-<op_id>-<hmac8>`. Only labels that verify may be deleted."""

from __future__ import annotations

import hashlib
import hmac
import re
import uuid

_RE = re.compile(r"^llmhub-([0-9a-f]{12})-([0-9a-f]{8})$")


def new_op_id() -> str:
    return uuid.uuid4().hex[:12]


def _mac(op_id: str, secret: bytes) -> str:
    return hmac.new(secret, op_id.encode(), hashlib.sha256).hexdigest()[:8]


def make_label(op_id: str, secret: bytes) -> str:
    return f"llmhub-{op_id}-{_mac(op_id, secret)}"


def parse_label(label: str) -> tuple[str, str] | None:
    m = _RE.match(label)
    return (m.group(1), m.group(2)) if m else None


def verify_label(label: str, secret: bytes) -> bool:
    parsed = parse_label(label)
    if parsed is None:
        return False
    op_id, mac = parsed
    return hmac.compare_digest(mac, _mac(op_id, secret))
