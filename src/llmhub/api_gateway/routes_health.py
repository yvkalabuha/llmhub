"""/healthz, /readyz, /metrics."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from llmhub.api_gateway.auth import require_admin, runtime
from llmhub.api_gateway.metrics import render_metrics
from llmhub.persistence import state as cstate

router = APIRouter()

READY_HEARTBEAT_S = 60.0


@router.get("/healthz")
async def healthz(request: Request) -> JSONResponse:
    rt = runtime(request)
    return JSONResponse(
        {
            "status": "ok",
            "version": rt.version,
            "halted": cstate.is_halted(rt.conn),
            "uptime_s": max(0.0, rt.clock.now() - rt.started_at),
        }
    )


@router.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    rt = runtime(request)
    now = rt.clock.now()
    try:
        beat = cstate.get(rt.conn, "heartbeat")
    except (sqlite3.Error, ValueError):
        return JSONResponse({"ready": False, "reason": "db"}, status_code=503)
    if beat is None or now - float(beat) > READY_HEARTBEAT_S:
        return JSONResponse({"ready": False, "reason": "controller_heartbeat"}, status_code=503)
    return JSONResponse({"ready": True})


@router.get("/metrics", dependencies=[Depends(require_admin)])
async def metrics(request: Request) -> PlainTextResponse:
    return PlainTextResponse(
        render_metrics(runtime(request)), media_type="text/plain; version=0.0.4"
    )
