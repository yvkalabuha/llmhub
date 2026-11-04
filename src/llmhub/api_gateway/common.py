"""Shared helpers of the route modules."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from llmhub.api_gateway.auth import Principal, runtime
from llmhub.api_gateway.errors import ApiError, ErrorCode
from llmhub.api_gateway.results import (
    job_error,
    response_headers,
    to_chat_completion,
)
from llmhub.model_router.views import retry_after_s


async def read_body(request: Request, *, allow_empty: bool = False) -> Any:
    """JSON body with the size limit; parsing runs in a thread (large bodies)."""
    limit = runtime(request).cfg.api.max_body_bytes
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(ErrorCode.REQUEST_TOO_LARGE, "request body too large")
        chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw.strip():
        if allow_empty:
            return {}
        raise ApiError(ErrorCode.INVALID_REQUEST, "request body is required")
    try:
        return await asyncio.to_thread(json.loads, raw)
    except ValueError:
        raise ApiError(ErrorCode.INVALID_REQUEST, "request body is not valid JSON") from None


async def read_object(request: Request, *, allow_empty: bool = False) -> dict[str, Any]:
    body = await read_body(request, allow_empty=allow_empty)
    if not isinstance(body, dict):
        raise ApiError(ErrorCode.INVALID_REQUEST, "request body must be a JSON object")
    return body


def split_llmhub(req: dict[str, Any]) -> dict[str, Any]:
    """Pop the optional `llmhub` object (validated: only known keys)."""
    meta = req.pop("llmhub", None)
    if meta is None:
        return {}
    known = {"priority", "policy", "deadline_s", "task_type", "data_class"}
    if not isinstance(meta, dict) or set(meta) - known:
        raise ApiError(ErrorCode.INVALID_REQUEST, "llmhub must be an object with known keys")
    return dict(meta)


def own_job(request: Request, principal: Principal, job_id: str) -> sqlite3.Row:
    row: sqlite3.Row | None = (
        runtime(request)
        .conn.execute(
            "SELECT * FROM jobs WHERE id=? AND client_id=?", (job_id, principal.client_id)
        )
        .fetchone()
    )
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "task not found")
    return row


def job_response(request: Request, job: sqlite3.Row) -> JSONResponse:
    """Final HTTP answer for a terminal job (chat.completion or the job's error)."""
    rt = runtime(request)
    if job["status"] == "completed":
        try:
            body = rt.bodies.read_result(job["id"])
        except OSError:
            raise ApiError(
                ErrorCode.INTERNAL_ERROR, "result is no longer available", task_id=job["id"]
            ) from None
        return JSONResponse(to_chat_completion(job, body), headers=response_headers(job))
    raise job_error(job)


def not_finished(request: Request, job: sqlite3.Row, code: ErrorCode) -> ApiError:
    rt = runtime(request)
    after = retry_after_s(rt.conn, rt.cfg, job, rt.worker, rt.clock.now())
    return ApiError(
        code, "task is not finished yet", retry_after_s=float(after), task_id=str(job["id"])
    )
