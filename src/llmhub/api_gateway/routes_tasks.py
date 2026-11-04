"""/v1/tasks: submit, status, result, cancel."""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from llmhub.api_gateway.auth import Principal, require_client, runtime
from llmhub.api_gateway.common import (
    job_response,
    not_finished,
    own_job,
    read_object,
    split_llmhub,
)
from llmhub.api_gateway.errors import ErrorCode
from llmhub.api_gateway.options import parse_options
from llmhub.api_gateway.results import backend_of, job_error
from llmhub.api_gateway.waiter import TERMINAL
from llmhub.task_queue import queue
from llmhub.task_queue.models import Priority

router = APIRouter()


def _task_view(job: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": job["id"], "object": "llmhub.task", "status": job["status"],
        "route": job["route"], "backend": backend_of(job),
        "created_at": job["created_at"],
    }  # fmt: skip


@router.post("/v1/tasks")
async def submit_task(
    request: Request, principal: Annotated[Principal, Depends(require_client)]
) -> Response:
    rt = runtime(request)
    req = await read_object(request)
    overrides = split_llmhub(req)
    opts = parse_options(request.headers, principal, rt.cfg, overrides)
    submitted = await rt.submitter.submit(principal, req, opts)
    job = own_job(request, principal, submitted.job_id)
    return JSONResponse(
        _task_view(job),
        status_code=202 if submitted.created else 200,
        headers={"Location": f"/v1/tasks/{job['id']}"},
    )


def _eta(request: Request, job: sqlite3.Row) -> float | None:
    rt = runtime(request)
    if job["status"] not in ("queued", "submitted", "running"):
        return None
    tin, tout = int(job["est_tokens_in"]), int(job["est_tokens_out"])
    now = rt.clock.now()
    if job["pool"] is None:
        return rt.worker.eta_s(job["model_alias"], tin, tout, int(job["prio_rank"]), now)
    view = rt.cloud.view(job["model_alias"], tin, tout, Priority(job["priority"]))
    return float(view.eta_s)


@router.get("/v1/tasks/{task_id}")
async def task_status(
    task_id: str, request: Request, principal: Annotated[Principal, Depends(require_client)]
) -> JSONResponse:
    job = own_job(request, principal, task_id)
    error = None
    if job["status"] in ("failed", "cancelled"):
        error = job_error(job).body()["error"]
    return JSONResponse(
        {
            **_task_view(job),
            "pool": job["pool"], "priority": job["priority"], "policy": job["policy"],
            "started_at": job["started_at"], "finished_at": job["finished_at"],
            "eta_s": _eta(request, job), "est_cost_usd": job["est_cost_usd"],
            "cost_usd": job["cost_usd"], "finish_reason": job["finish_reason"],
            "usage": None if job["tokens_in"] is None else {
                "prompt_tokens": job["tokens_in"], "completion_tokens": job["tokens_out"]},
            "fallback": job["fallback_reason"],
            "warnings": [w for w in (job["warnings"] or "").split(",") if w],
            "error": error,
        }
    )  # fmt: skip


@router.get("/v1/tasks/{task_id}/result")
async def task_result(
    task_id: str, request: Request, principal: Annotated[Principal, Depends(require_client)]
) -> Response:
    job = own_job(request, principal, task_id)
    if job["status"] not in TERMINAL:
        raise not_finished(request, job, ErrorCode.TASK_NOT_READY)
    return job_response(request, job)


@router.post("/v1/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str, request: Request, principal: Annotated[Principal, Depends(require_client)]
) -> JSONResponse:
    rt = runtime(request)
    own_job(request, principal, task_id)
    status = queue.cancel(rt.conn, task_id, rt.clock.now())
    job = own_job(request, principal, task_id)
    if status.value == "running":
        if job["pool"] is None:
            rt.worker.cancel(task_id)  # breaks the upstream call; the worker finishes it
        rt.controller.wake()
    rt.waiter.notify(task_id)
    return JSONResponse(
        {"id": task_id, "status": job["status"], "cancel_requested": bool(job["cancel_requested"])}
    )
