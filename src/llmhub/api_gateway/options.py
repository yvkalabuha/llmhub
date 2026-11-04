"""X-LLMHub-* headers and the optional `llmhub` body object -> RequestOptions."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast, get_args

from llmhub.api_gateway.auth import Principal
from llmhub.api_gateway.errors import ApiError, ErrorCode
from llmhub.configuration.api_models import Policy
from llmhub.configuration.models import Config
from llmhub.task_queue.models import Priority

DataClass = Literal["private", "cloud_allowed"]
_POLICIES: tuple[str, ...] = get_args(Policy)


@dataclass(frozen=True)
class RequestOptions:
    priority: Priority
    policy: Policy
    deadline_in_s: float | None
    wait_s: float
    task_type: str | None
    data_class: DataClass
    lease_id: str | None
    idem_key: str | None


def _bad(msg: str) -> ApiError:
    return ApiError(ErrorCode.INVALID_REQUEST, msg)


def _num(raw: object, name: str) -> float:
    try:
        value = float(cast(Any, raw)) if not isinstance(raw, bool) else math.nan
    except (TypeError, ValueError):
        raise _bad(f"{name} must be a number") from None
    if math.isnan(value):
        raise _bad(f"{name} must be a number")
    if math.isinf(value):
        raise _bad(f"{name} must be finite")
    return value


def _data_class(raw: object, name: str) -> DataClass:
    if raw not in ("private", "cloud_allowed"):
        raise _bad(f"{name} must be 'private' or 'cloud_allowed'")
    return cast(DataClass, raw)


def parse_options(
    headers: Mapping[str, str],
    principal: Principal,
    cfg: Config,
    overrides: Mapping[str, Any] | None = None,
) -> RequestOptions:
    """`overrides` (the body `llmhub` object) beat headers. data_class can only narrow:
    effective = client AND header AND field."""
    h = {k.lower(): v for k, v in headers.items()}
    ov = dict(overrides or {})

    prio_raw = ov.get("priority", h.get("x-llmhub-priority"))
    if prio_raw is None:
        priority = principal.default_priority
    else:
        try:
            priority = Priority(str(prio_raw).lower())
        except ValueError:
            raise _bad("priority must be urgent, normal or low") from None
    if priority.rank < principal.max_priority.rank:
        raise ApiError(
            ErrorCode.FORBIDDEN, "priority above the client limit", reason="priority_not_allowed"
        )

    pol_raw = ov.get("policy", h.get("x-llmhub-policy"))
    if pol_raw is None:
        policy = principal.default_policy
    else:
        if str(pol_raw).upper() not in _POLICIES:
            raise _bad("policy must be ECONOMY, BALANCED or QUALITY")
        policy = cast(Policy, str(pol_raw).upper())

    dl_raw = ov.get("deadline_s", h.get("x-llmhub-deadline"))
    deadline: float | None = None
    if dl_raw is not None:
        deadline = _num(dl_raw, "deadline")
        if deadline <= 0:
            raise _bad("deadline must be positive")

    wait_raw = h.get("x-llmhub-wait")
    wait = cfg.api.sync_wait_s
    if wait_raw is not None:
        wait = _num(wait_raw, "X-LLMHub-Wait")
        if not 1 <= wait <= cfg.api.sync_wait_s:
            raise _bad(f"X-LLMHub-Wait must be within 1..{cfg.api.sync_wait_s:g}")

    tt_raw = ov.get("task_type", h.get("x-llmhub-task-type"))
    task_type: str | None = None
    if tt_raw is not None:
        task_type = str(tt_raw)
        if task_type not in cfg.task_types:
            raise _bad("unknown task type")

    data_class: DataClass = "cloud_allowed" if principal.cloud_allowed else "private"
    for raw, name in ((h.get("x-llmhub-data-class"), "X-LLMHub-Data-Class"),
                      (ov.get("data_class"), "llmhub.data_class")):  # fmt: skip
        if raw is not None and _data_class(raw, name) == "private":
            data_class = "private"

    idem = h.get("idempotency-key")
    if idem is not None and (
        not 1 <= len(idem) <= 255 or not all(0x21 <= ord(c) <= 0x7E for c in idem)
    ):
        raise _bad("Idempotency-Key must be 1..255 printable ASCII characters")

    return RequestOptions(
        priority=priority, policy=policy, deadline_in_s=deadline, wait_s=wait,
        task_type=task_type, data_class=data_class, lease_id=h.get("x-llmhub-lease"),
        idem_key=idem,
    )  # fmt: skip
