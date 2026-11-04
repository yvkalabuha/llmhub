"""Request -> job: alias, secrets, router decision, canonical body, idempotent submit."""

from __future__ import annotations

import asyncio
import json
import math
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from llmhub.api_gateway.auth import Principal
from llmhub.api_gateway.errors import ApiError, ErrorCode
from llmhub.api_gateway.options import RequestOptions
from llmhub.budget_manager import limits
from llmhub.model_router.rules import (
    LocalView,
    RouteDecision,
    RouteError,
    RouteRequest,
    decide,
    resolve_alias,
)
from llmhub.model_router.secrets import detect_secrets
from llmhub.persistence.db import tx
from llmhub.task_queue.models import IdempotencyConflict, JobSpec

if TYPE_CHECKING:
    from llmhub.api_gateway.app import Runtime

_DROP = ("stream", "stream_options", "llmhub")


def canonical_body(req: dict[str, Any]) -> bytes:
    """Same bytes for stream and non-stream repeats: no stream fields, sorted keys."""
    body = {k: v for k, v in req.items() if k not in ("stream", "stream_options")}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def estimate_tokens_in(req: dict[str, Any], bytes_per_token: float) -> int:
    """Conservative size estimate: UTF-8 bytes of messages and tools / bytes_per_token."""
    parts = {k: req[k] for k in ("messages", "tools", "response_format", "system") if k in req}
    size = len(json.dumps(parts, ensure_ascii=False).encode())
    return max(1, math.ceil(size / bytes_per_token))


def _prepare(
    req: dict[str, Any], meta: dict[str, Any], bytes_per_token: float
) -> tuple[bytes, int]:
    """Heavy pure CPU work (runs in a worker thread)."""
    body = {k: v for k, v in req.items() if k not in _DROP}
    body["_llmhub"] = meta
    return canonical_body(body), estimate_tokens_in(req, bytes_per_token)


@dataclass(frozen=True)
class Submitted:
    job_id: str
    created: bool
    decision: RouteDecision | None


def route_error_to_api(exc: RouteError) -> ApiError:
    code = ErrorCode(str(exc.code))
    if code is ErrorCode.CLOUD_UNAVAILABLE and exc.reason == "controller_halted":
        code = ErrorCode.CONTROLLER_HALTED
    msgs = {
        ErrorCode.FORBIDDEN: "request is not allowed to use this model or route",
        ErrorCode.CONTEXT_TOO_LARGE: "request does not fit the model context",
        ErrorCode.MODEL_NOT_AVAILABLE: "model is not available",
        ErrorCode.CLOUD_UNAVAILABLE: "cloud GPU is unavailable",
        ErrorCode.LOCAL_UNAVAILABLE: "local model is unavailable",
        ErrorCode.CONTROLLER_HALTED: "service is halted",
    }
    return ApiError(
        code, msgs.get(code, str(code.value)), reason=exc.reason, retry_after_s=exc.retry_after_s
    )


class Submitter:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    # --- helpers ---------------------------------------------------------------
    def _check_lease(self, principal: Principal, lease_id: str, now: float) -> None:
        row: sqlite3.Row | None = self.rt.conn.execute(
            "SELECT client_id, closed_at, close_reason, expires_at FROM leases WHERE id=?",
            (lease_id,),
        ).fetchone()
        if row is None or row["client_id"] != principal.client_id:
            raise ApiError(ErrorCode.NOT_FOUND, "lease not found")
        if (row["closed_at"] is not None and row["close_reason"] == "expired") or (
            row["closed_at"] is None and row["expires_at"] is not None and row["expires_at"] <= now
        ):
            raise ApiError(ErrorCode.LEASE_EXPIRED, "lease expired")
        if row["closed_at"] is not None:
            raise ApiError(ErrorCode.LEASE_CLOSED, "lease closed")

    def _local_view(
        self, alias: str, tin: int, max_tokens: int, prio_rank: int, now: float
    ) -> LocalView:
        cfg = self.rt.cfg
        name = cfg.models[alias].local if alias in cfg.models else None
        if name is None:
            return LocalView(backend=None, ctx_tokens=None, eta_s=None, available=False)
        worker = self.rt.worker
        return LocalView(
            backend=name,
            ctx_tokens=cfg.local_backends[name].ctx_tokens,
            eta_s=worker.eta_s(alias, tin, max_tokens, prio_rank, now),
            available=worker.available(name),
        )

    # --- main ------------------------------------------------------------------
    async def submit(
        self, principal: Principal, req: dict[str, Any], opts: RequestOptions
    ) -> Submitted:
        rt, cfg = self.rt, self.rt.cfg
        model = req.get("model")
        if not isinstance(model, str) or not model:
            raise ApiError(ErrorCode.INVALID_REQUEST, "model is required")
        if not isinstance(req.get("messages"), list) or not req["messages"]:
            raise ApiError(ErrorCode.INVALID_REQUEST, "messages must be a non-empty list")
        now = rt.clock.now()
        if opts.lease_id is not None:
            self._check_lease(principal, opts.lease_id, now)

        # budget stage: normal/low priority work goes ECONOMY from 80 %
        policy = opts.policy
        warnings: list[str] = []
        if (
            opts.priority.rank > 0
            and policy != "ECONOMY"
            and limits.stage(rt.conn, now, cfg) >= limits.Stage.ECONOMY
        ):
            policy = "ECONOMY"
            warnings.append("economy_stage")

        try:
            alias = resolve_alias(
                model,
                policy,
                cfg,
                principal.may_use,
                cloud_allowed=opts.data_class == "cloud_allowed",
            )
        except RouteError as exc:
            raise route_error_to_api(exc) from None

        max_tokens = self._max_tokens(req, opts)
        meta = {"data_class": opts.data_class, "policy": policy, "priority": opts.priority.value}
        body_req = dict(req)
        body_req["model"] = alias
        (body, tokens_in), hits = await asyncio.gather(
            asyncio.to_thread(_prepare, body_req, meta, cfg.router.bytes_per_token),
            asyncio.to_thread(detect_secrets, req, cfg.router.extra_secret_patterns),
        )
        if hits:
            warnings.append("secret_detected")

        local = self._local_view(alias, tokens_in, max_tokens, opts.priority.rank, now)
        cloud = rt.cloud.view(alias, tokens_in, max_tokens, opts.priority)
        try:
            decision = decide(
                RouteRequest(
                    alias=alias, task_type=opts.task_type, priority=opts.priority, policy=policy,
                    deadline_in_s=opts.deadline_in_s, data_class=opts.data_class,
                    secret_hits=tuple(hits), tokens_in=tokens_in, max_tokens=max_tokens,
                ),
                local, cloud, cfg.router,
            )  # fmt: skip
        except RouteError as exc:
            raise route_error_to_api(exc) from None

        # private data and detected secrets never reach the cloud (blocker invariant)
        if decision.route == "cloud" and (opts.data_class == "private" or hits):
            raise ApiError(
                ErrorCode.FORBIDDEN, "route to cloud refused", reason="private_requires_local"
            )

        # X-LLMHub-Fallback (T-369 I): a cloud-eligible request (cloud_allowed, no secrets,
        # alias with a pool) served locally while the cloud is unavailable carries the reason,
        # whichever rule picked the local route. Private/secret requests never had the cloud
        # as an option, so they get no fallback mark.
        fallback = decision.fallback
        if (
            fallback is None
            and decision.route == "local"
            and opts.data_class == "cloud_allowed"
            and not hits
            and cloud.pool is not None
            and cloud.unavailable is not None
        ):
            fallback = str(cloud.unavailable)
        local_fallback = decision.route == "local" and fallback is not None
        spec = JobSpec(
            client_id=principal.client_id, body=body, priority=opts.priority, model_alias=alias,
            policy=policy, data_class=opts.data_class,
            route="cloud" if decision.route == "cloud" else (
                "local:fallback" if local_fallback else "local"),
            pool=decision.pool if decision.route == "cloud" else None,
            route_reason=decision.reason, task_type=opts.task_type, idem_key=opts.idem_key,
            deadline_at=None if opts.deadline_in_s is None else now + opts.deadline_in_s,
            est_tokens_in=tokens_in, max_tokens=max_tokens, est_cost_usd=decision.est_cost_usd,
        )  # fmt: skip
        try:
            result = rt.controller.submit(spec)
        except IdempotencyConflict as exc:
            raise ApiError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Idempotency-Key reused with another body",
                task_id=exc.job_id,
            ) from None
        if not result.created:
            return Submitted(result.job_id, False, None)

        all_warnings = [*warnings, *decision.warnings]
        backend: str | None = None
        if decision.route == "local" and decision.backend:
            backend = (
                decision.backend
                if decision.backend.startswith("local:")
                else (f"local:{decision.backend}")
            )
        with tx(rt.conn):
            rt.conn.execute(
                "UPDATE jobs SET warnings=?, backend=COALESCE(?, backend),"
                " fallback_reason=COALESCE(?, fallback_reason) WHERE id=?",
                (",".join(dict.fromkeys(all_warnings)) or None, backend, fallback,
                 result.job_id),
            )  # fmt: skip
        if decision.route == "local":
            rt.worker.kick()
        return Submitted(result.job_id, True, decision)

    def _max_tokens(self, req: dict[str, Any], opts: RequestOptions) -> int:
        cfg = self.rt.cfg
        for key in ("max_completion_tokens", "max_tokens"):
            v = req.get(key)
            if isinstance(v, int) and not isinstance(v, bool) and v > 0:
                return v
            if v is not None:
                raise ApiError(ErrorCode.INVALID_REQUEST, f"{key} must be a positive integer")
        if opts.task_type is not None and opts.task_type in cfg.task_types:
            return cfg.task_types[opts.task_type].max_tokens
        return cfg.task_type_max_tokens["default"]
