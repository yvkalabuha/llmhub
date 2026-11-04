"""POST /v1/chat/completions and GET /v1/models."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from llmhub.api_gateway.auth import Principal, require_client, runtime
from llmhub.api_gateway.common import (
    job_response,
    not_finished,
    read_object,
    split_llmhub,
)
from llmhub.api_gateway.errors import ApiError, ErrorCode
from llmhub.api_gateway.options import parse_options
from llmhub.api_gateway.results import (
    job_error,
    response_headers,
    sse_event,
    to_chat_completion,
    to_sse_chunks,
)
from llmhub.api_gateway.waiter import TERMINAL

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request, principal: Annotated[Principal, Depends(require_client)]
) -> Response:
    rt = runtime(request)
    req = await read_object(request)
    overrides = split_llmhub(req)
    opts = parse_options(request.headers, principal, rt.cfg, overrides)
    stream = req.get("stream") is True
    options = req.get("stream_options")
    include_usage = isinstance(options, dict) and options.get("include_usage") is True
    submitted = await rt.submitter.submit(principal, req, opts)
    until = rt.clock.now() + opts.wait_s
    if not stream:
        job = await rt.waiter.wait(submitted.job_id, until, request.is_disconnected)
        if job is None:
            return Response(status_code=204)  # the client is gone; the task keeps running
        if job["status"] not in TERMINAL:
            raise not_finished(request, job, ErrorCode.QUEUE_WAIT_EXCEEDED)
        return job_response(request, job)

    first = rt.conn.execute("SELECT * FROM jobs WHERE id=?", (submitted.job_id,)).fetchone()

    async def events() -> AsyncIterator[str]:
        yield ": keep-alive\n\n"
        job: sqlite3.Row | None
        while True:
            step = min(rt.cfg.api.keepalive_s, max(0.0, until - rt.clock.now()))
            job = await rt.waiter.wait(
                submitted.job_id, rt.clock.now() + step, request.is_disconnected
            )
            if job is None:
                return
            if job["status"] in TERMINAL:
                break
            if rt.clock.now() >= until:
                err = not_finished(request, job, ErrorCode.QUEUE_WAIT_EXCEEDED)
                yield sse_event(err.body())
                return
            yield ": keep-alive\n\n"
        if job["status"] != "completed":
            yield sse_event(job_error(job).body())
            return
        try:
            body = rt.bodies.read_result(job["id"])
        except OSError:
            yield sse_event(
                ApiError(ErrorCode.INTERNAL_ERROR, "result is no longer available").body()
            )
            return
        for chunk in to_sse_chunks(to_chat_completion(job, body), include_usage):
            yield chunk

    headers = response_headers(first)
    headers["Cache-Control"] = "no-cache"
    headers["X-Accel-Buffering"] = "no"
    return StreamingResponse(events(), media_type="text/event-stream", headers=headers)


@router.get("/v1/models")
async def list_models(
    request: Request, principal: Annotated[Principal, Depends(require_client)]
) -> JSONResponse:
    rt = runtime(request)
    cfg = rt.cfg
    created = int(rt.started_at)
    cloud_aliases = {a for p in cfg.pools.values() for a in p.models}

    def entry(alias: str) -> dict[str, Any]:
        m = cfg.models[alias]
        ctx = [m.cloud_ctx_tokens] if alias in cloud_aliases else []
        if m.local is not None:
            ctx.append(cfg.local_backends[m.local].ctx_tokens)
        return {
            "id": alias, "object": "model", "created": created, "owned_by": "llmhub",
            "llmhub": {
                "local": m.local is not None, "cloud": alias in cloud_aliases,
                "ctx_tokens": max(ctx) if ctx else None,
            },
        }  # fmt: skip

    data = [entry(a) for a in sorted(cfg.models) if principal.may_use(a)]
    auto = cfg.router.auto_alias
    if auto is not None and principal.may_use(auto):
        data.append(
            {"id": "auto", "object": "model", "created": created, "owned_by": "llmhub",
             "llmhub": {**entry(auto)["llmhub"], "alias_of": auto}}
        )  # fmt: skip
    return JSONResponse({"object": "list", "data": data})
