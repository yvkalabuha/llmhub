"""/v1/leases: explicit/prewarm leases of a client."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from llmhub.api_gateway.auth import Principal, require_client, runtime
from llmhub.api_gateway.common import read_object
from llmhub.api_gateway.errors import ApiError, ErrorCode
from llmhub.lifecycle_manager import leases
from llmhub.persistence.db import tx

router = APIRouter()


def _own_lease(request: Request, principal: Principal, lease_id: str) -> sqlite3.Row:
    row: sqlite3.Row | None = (
        runtime(request)
        .conn.execute(
            "SELECT * FROM leases WHERE id=? AND client_id=? AND kind IN ('explicit','prewarm')",
            (lease_id, principal.client_id),
        )
        .fetchone()
    )
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "lease not found")
    return row


def _number(body: dict[str, Any], key: str) -> float | None:
    v = body.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ApiError(ErrorCode.INVALID_REQUEST, f"{key} must be a number")
    return float(v)


@router.post("/v1/leases")
async def create_lease(
    request: Request, principal: Annotated[Principal, Depends(require_client)]
) -> JSONResponse:
    rt = runtime(request)
    body = await read_object(request)
    model = body.get("model")
    if not isinstance(model, str) or model not in rt.cfg.models:
        raise ApiError(ErrorCode.MODEL_NOT_AVAILABLE, "model is not available")
    if not principal.may_use(model):
        raise ApiError(ErrorCode.FORBIDDEN, "model is not allowed for this client")
    if not principal.cloud_allowed:
        raise ApiError(
            ErrorCode.FORBIDDEN, "client may not use the cloud", reason="cloud_not_allowed"
        )
    pool = next((n for n, p in rt.cfg.pools.items() if model in p.models), None)
    if pool is None:
        raise ApiError(ErrorCode.NOT_FOUND, "model has no cloud pool")
    ttl = _number(body, "ttl_s")
    prewarm = body.get("prewarm", False)
    if not isinstance(prewarm, bool):
        raise ApiError(ErrorCode.INVALID_REQUEST, "prewarm must be a boolean")
    now = rt.clock.now()
    try:
        with tx(rt.conn):
            lease_id = leases.create_explicit(
                rt.conn, holder=f"client:{principal.client_id}", client_id=principal.client_id,
                pool=pool, ttl_s=ttl, now=now, prewarm=prewarm, cfg=rt.cfg.leases,
            )  # fmt: skip
    except leases.LeaseError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from None
    row = _own_lease(request, principal, lease_id)
    if prewarm:
        rt.controller.wake()
    return JSONResponse(
        {"id": lease_id, "pool": pool, "kind": row["kind"], "ttl_s": row["ttl_s"],
         "expires_at": row["expires_at"]},
        status_code=201,
    )  # fmt: skip


@router.post("/v1/leases/{lease_id}/renew")
async def renew_lease(
    lease_id: str, request: Request, principal: Annotated[Principal, Depends(require_client)]
) -> JSONResponse:
    rt = runtime(request)
    row = _own_lease(request, principal, lease_id)
    if not principal.cloud_allowed:  # revoked by a reload (SEC-M3): the GPU is not kept alive
        raise ApiError(
            ErrorCode.FORBIDDEN, "client may not use the cloud", reason="cloud_not_allowed"
        )
    if row["closed_at"] is not None and row["close_reason"] == "expired":
        raise ApiError(ErrorCode.LEASE_EXPIRED, "lease expired")
    try:
        with tx(rt.conn):
            expires = leases.renew(rt.conn, lease_id, rt.clock.now())
    except leases.LeaseExpired:
        raise ApiError(ErrorCode.LEASE_EXPIRED, "lease expired") from None
    except leases.LeaseClosed:
        raise ApiError(ErrorCode.LEASE_CLOSED, "lease closed") from None
    return JSONResponse({"id": lease_id, "expires_at": expires})


@router.post("/v1/leases/{lease_id}/close")
async def close_lease(
    lease_id: str, request: Request, principal: Annotated[Principal, Depends(require_client)]
) -> JSONResponse:
    rt = runtime(request)
    _own_lease(request, principal, lease_id)
    body = await read_object(request, allow_empty=True)
    final = body.get("final", False)
    if not isinstance(final, bool):
        raise ApiError(ErrorCode.INVALID_REQUEST, "final must be a boolean")
    with tx(rt.conn):
        leases.close(rt.conn, lease_id, rt.clock.now(), final=final, reason="client")
    rt.controller.wake()
    return JSONResponse({"id": lease_id, "closed": True})
