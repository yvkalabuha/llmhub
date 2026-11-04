"""Weights-server tokens (T-370 stream D, ADR-0138 item 4 as interpreted in plan 2a).

One token per instance, reusable for Range requests and retries, valid while the instance is
loading and revoked as soon as its state leaves LOADING_STATES or `exp` passes. No nonce.
Signature: HMAC-SHA256(secret, "llmhub-weights|v1|" + op_id + "|" + exp), url-safe base64.
"""

from __future__ import annotations

import base64
import enum
import hashlib
import hmac
import math
import re
from collections.abc import Callable

from llmhub.configuration.models import TimeoutsConfig

TOKEN_VERSION = "v1"
LOADING_STATES = frozenset({"PROVISIONING", "BOOTING", "LOADING_MODEL"})
_PREFIX = f"llmhub-weights|{TOKEN_VERSION}|"
_OP = r"[0-9a-f]{12}"
_TOKEN_RE = re.compile(rf"^{TOKEN_VERSION}\.({_OP})\.([0-9]{{1,12}})\.([A-Za-z0-9_-]{{43}})$")
_PATH_RE = re.compile(rf"^/w/({_OP})/(.+)$")
_BEARER_RE = re.compile(r"^Bearer ([^\s]+)$")


class Verdict(enum.StrEnum):
    OK = "ok"
    MISSING = "missing"
    MALFORMED = "malformed"
    BAD_SIG = "bad_signature"
    EXPIRED = "expired"
    OP_MISMATCH = "op_mismatch"
    UNKNOWN_OP = "unknown_op"
    REVOKED = "revoked"
    PATH = "path_not_allowed"
    # raised by the validator app, not by check_request
    IP_MISMATCH = "ip_mismatch"
    VOLUME_CAP = "volume_cap"
    DB_BUSY = "db_busy"


def token_expiry(created_at: float, t: TimeoutsConfig) -> int:
    return math.ceil(created_at + t.provisioning_s + t.booting_s + t.loading_model_s)


def _sig(secret: bytes, op_id: str, exp: int) -> str:
    mac = hmac.new(secret, f"{_PREFIX}{op_id}|{exp}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def issue_token(secret: bytes, op_id: str, exp: int) -> str:
    return f"{TOKEN_VERSION}.{op_id}.{exp}.{_sig(secret, op_id, exp)}"


def split_uri(uri: str) -> tuple[str, str] | None:
    """`/w/<op>/<rel>` -> (op, rel); None for any other shape."""
    m = _PATH_RE.match(uri)
    return (m.group(1), m.group(2)) if m else None


def check_request(
    secret: bytes,
    authorization: str | None,
    uri: str,
    now: float,
    state_of: Callable[[str], str | None],
    allowed: frozenset[str],
) -> tuple[Verdict, str | None]:
    """`uri` is the RAW request target (nginx `$request_uri`).
    Order: format -> HMAC -> exp -> op in path -> path -> instance state (only then the DB)."""
    if authorization is None or authorization.strip() == "":
        return Verdict.MISSING, None
    bm = _BEARER_RE.match(authorization)
    if bm is None:
        return Verdict.MALFORMED, None
    tm = _TOKEN_RE.match(bm.group(1))
    if tm is None:
        return Verdict.MALFORMED, None
    op_id, exp_s, sig = tm.groups()
    exp = int(exp_s)
    if not hmac.compare_digest(sig.encode(), _sig(secret, op_id, exp).encode()):
        return Verdict.BAD_SIG, None
    if now >= exp:
        return Verdict.EXPIRED, op_id
    parts = split_uri(uri)
    if parts is None:
        return Verdict.PATH, op_id
    path_op, rel = parts
    if path_op != op_id:
        return Verdict.OP_MISMATCH, op_id
    if rel not in allowed or ".." in rel or "%" in rel or "//" in rel or "\\" in rel or "?" in rel:
        return Verdict.PATH, op_id
    state = state_of(op_id)
    if state is None:
        return Verdict.UNKNOWN_OP, op_id
    if state not in LOADING_STATES:
        return Verdict.REVOKED, op_id
    return Verdict.OK, op_id
