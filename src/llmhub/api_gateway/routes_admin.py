"""/admin/*: operator API (admin key only)."""

from __future__ import annotations

import dataclasses
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from llmhub.api_gateway.auth import require_admin, runtime
from llmhub.api_gateway.common import read_object
from llmhub.api_gateway.errors import ApiError, ErrorCode
from llmhub.budget_manager import limits
from llmhub.configuration.loader import ConfigError
from llmhub.controller import HoldExceedsSessionBudget, HoldRejected, KillFilePresent
from llmhub.killswitch import check_kill_file
from llmhub.lifecycle_manager import leases
from llmhub.lifecycle_manager.machine import live_instance
from llmhub.persistence import state as cstate
from llmhub.persistence.state import ReconcileRequired

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _rows(request: Request, sql: str, *params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in runtime(request).conn.execute(sql, params).fetchall()]


def _budget(request: Request) -> dict[str, Any]:
    rt = runtime(request)
    now = rt.clock.now()
    w = limits.windows(now, rt.cfg.budget.timezone)
    b = rt.cfg.budget
    return {
        "stage": limits.stage(rt.conn, now, rt.cfg).name,
        "month": {"key": w.month_key, "committed_usd": limits.committed(
            rt.conn, w.month_start, w.month_end, now), "budget_usd": b.budget_month,
            "hard_cap_usd": b.hard_cap_month},
        "day": {"key": w.day_key, "committed_usd": limits.committed(
            rt.conn, w.day_start, w.day_end, now), "budget_usd": b.budget_day},
        "warnings": limits.warnings(rt.conn, now, rt.cfg),
    }  # fmt: skip


@router.get("/status")
async def status(request: Request) -> JSONResponse:
    rt = runtime(request)
    pools: dict[str, Any] = {}
    now = rt.clock.now()
    for name in rt.cfg.pools:
        inst = live_instance(rt.conn, name)
        queue = {
            r["priority"]: r["n"]
            for r in _rows(
                request,
                "SELECT priority, count(*) AS n FROM jobs WHERE pool=? AND status='queued'"
                " GROUP BY priority", name,
            )
        }  # fmt: skip
        pools[name] = {
            "instance": None if inst is None else {
                "id": inst["id"], "state": inst["state"], "gpu": inst["gpu"],
                "price_usd_h": inst["price_usd_h"], "state_since": inst["state_since"]},
            "queue": queue,
            "leases": leases.live_count(rt.conn, name, now),
        }  # fmt: skip
    local: dict[str, Any] = {}
    for name, st in rt.worker.stats.items():
        local[name] = {
            "available": bool(st.available), "busy_external": bool(st.busy_external),
            "ewma_pp_tps": st.pp.value, "ewma_tg_tps": st.tg.value,
        }  # fmt: skip
    local_queue = _rows(
        request, "SELECT status, count(*) AS n FROM jobs WHERE pool IS NULL AND status IN"
        " ('queued','running') GROUP BY status",
    )  # fmt: skip
    return JSONResponse(
        {
            "halted": cstate.is_halted(rt.conn),
            "halt_reason": cstate.get(rt.conn, "halt_reason") or None,
            "safe_mode": cstate.get(rt.conn, "safe_mode") == "1",
            "kill_file": check_kill_file(rt.cfg.data_dir),
            "pools": pools,
            "local": {"backends": local, "queue": {r["status"]: r["n"] for r in local_queue}},
            "budget": _budget(request),
            "providers": _rows(
                request,
                "SELECT provider, enabled, blocked_reason, circuit_state,"
                " last_balance_usd FROM provider_accounts ORDER BY provider",
            ),
            "incidents": _rows(
                request,
                "SELECT id, ts, kind, severity, ref FROM incidents WHERE resolved_at"
                " IS NULL ORDER BY ts DESC LIMIT 20",
            ),
        }
    )


@router.get("/budget")
async def budget(request: Request) -> JSONResponse:
    rt = runtime(request)
    w = limits.windows(rt.clock.now(), rt.cfg.budget.timezone)
    ledger = _rows(
        request,
        "SELECT pool, kind, source, round(sum(amount_usd), 6) AS usd FROM ledger"
        " WHERE from_ts>=? AND from_ts<? GROUP BY pool, kind, source LIMIT 200",
        w.month_start, w.month_end,
    )  # fmt: skip
    return JSONResponse({**_budget(request), "ledger_month": ledger})


@router.get("/clients")
async def clients(request: Request) -> JSONResponse:
    rt = runtime(request)
    disabled = {
        r["id"]: r["disabled_at"] for r in _rows(request, "SELECT id, disabled_at FROM clients")
    }
    out = [
        {
            "name": name,
            "allowed_models": sorted(c.allowed_models),
            "max_priority": c.max_priority.value,
            "cloud_allowed": c.cloud_allowed,
            "disabled": disabled.get(name) is not None,
        }
        for name, c in sorted(rt.cfg.clients.items())
    ]
    return JSONResponse({"clients": out})


@router.post("/reload_clients")
async def reload_clients(request: Request) -> JSONResponse:
    rt = runtime(request)
    if rt.config_path is None:
        raise ApiError(ErrorCode.INVALID_REQUEST, "no config file to reload from")
    try:
        n = rt.registry.reload(rt.config_path)
    except ConfigError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, f"config rejected: {exc}"[:300]) from None
    return JSONResponse({"clients": n})


def _pool_of(request: Request, body: dict[str, Any]) -> str:
    cfg = runtime(request).cfg
    pool, model = body.get("pool"), body.get("model")
    if isinstance(pool, str):
        if pool not in cfg.pools:
            raise ApiError(ErrorCode.NOT_FOUND, "unknown pool")
        return pool
    if isinstance(model, str):
        found = next((n for n, p in cfg.pools.items() if model in p.models), None)
        if found is None:
            raise ApiError(ErrorCode.NOT_FOUND, "model has no cloud pool")
        return found
    raise ApiError(ErrorCode.INVALID_REQUEST, "pool or model is required")


@router.post("/hold")
async def hold(request: Request) -> JSONResponse:
    rt = runtime(request)
    body = await read_object(request)
    pool = _pool_of(request, body)
    ttl = body.get("ttl_s")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        raise ApiError(ErrorCode.INVALID_REQUEST, "ttl_s is required")
    try:
        lease_id = rt.controller.hold(pool, float(ttl))
    except HoldRejected as exc:
        reason = exc.gate.reason.value if exc.gate.reason is not None else "hold_rejected"
        raise ApiError(ErrorCode.CONFLICT, "hold rejected", reason=reason) from None
    except HoldExceedsSessionBudget:
        raise ApiError(ErrorCode.CONFLICT, "hold rejected", reason="budget_limit_reached") from None
    except leases.LeaseError as exc:
        raise ApiError(ErrorCode.INVALID_REQUEST, str(exc)) from None
    return JSONResponse({"lease_id": lease_id}, status_code=201)


@router.post("/release")
async def release(request: Request) -> JSONResponse:
    rt = runtime(request)
    pool = _pool_of(request, await read_object(request))
    rt.controller.release(pool)
    return JSONResponse({"pool": pool, "released": True})


@router.post("/kill")
async def kill(request: Request) -> JSONResponse:
    rt = runtime(request)
    body = await read_object(request, allow_empty=True)
    reason = str(body.get("reason") or "admin")[:100]
    report = await rt.controller.kill(reason)
    return JSONResponse(dataclasses.asdict(report))


@router.post("/resume")
async def resume(request: Request) -> JSONResponse:
    rt = runtime(request)
    body = await read_object(request, allow_empty=True)
    provider = body.get("provider")
    try:
        rt.controller.resume(provider if isinstance(provider, str) else None)
    except KillFilePresent:
        raise ApiError(
            ErrorCode.CONFLICT, "KILL file present", reason="kill_file_present"
        ) from None
    except ReconcileRequired:
        raise ApiError(
            ErrorCode.CONFLICT, "billing reload required", reason="reconcile_required"
        ) from None
    return JSONResponse({"halted": cstate.is_halted(rt.conn)})


@router.post("/reload_billing")
async def reload_billing(request: Request) -> JSONResponse:
    await runtime(request).controller.reload_billing()
    return JSONResponse({"reloaded": True})


@router.post("/clear_weights_hold")
async def clear_weights_hold(request: Request) -> JSONResponse:
    rt = runtime(request)
    pool = _pool_of(request, await read_object(request))
    return JSONResponse({"pool": pool, "removed": rt.controller.clear_weights_hold(pool)})
