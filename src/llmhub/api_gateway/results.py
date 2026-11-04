"""Job -> OpenAI objects: chat.completion, SSE chunks, response headers, error mapping."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from llmhub.api_gateway.errors import ApiError, ErrorCode, retryable_for
from llmhub.api_gateway.waiter import TERMINAL

__all__ = [
    "TERMINAL", "backend_of", "job_error", "response_headers", "sse_event", "to_chat_completion",
    "to_sse_chunks",
]  # fmt: skip

_KNOWN = {c.value: c for c in ErrorCode}


def backend_of(job: sqlite3.Row) -> str | None:
    if job["backend"]:
        return str(job["backend"])
    if job["pool"]:
        return f"cloud:{job['pool']}"
    return None


def _created(job: sqlite3.Row) -> int:
    return int(job["finished_at"] or job["created_at"])


def to_chat_completion(job: sqlite3.Row, body: bytes) -> dict[str, Any]:
    """llama-server JSON as is (id and model replaced); a non-JSON body (Mock) is wrapped."""
    parsed: Any = None
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = None
    tin, tout = job["tokens_in"] or 0, job["tokens_out"] or 0
    if isinstance(parsed, dict) and isinstance(parsed.get("choices"), list):
        out: dict[str, Any] = dict(parsed)
    else:
        out = {
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": body.decode(errors="replace")},
                    "finish_reason": job["finish_reason"] or "stop",
                }
            ]
        }
    out["id"] = f"chatcmpl-{job['id']}"
    out["object"] = "chat.completion"
    out["created"] = _created(job)
    out["model"] = job["model_alias"]
    out.setdefault(
        "usage",
        {"prompt_tokens": tin, "completion_tokens": tout, "total_tokens": tin + tout},
    )
    return out


def sse_event(obj: dict[str, Any]) -> str:
    return "data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def to_sse_chunks(completion: dict[str, Any], include_usage: bool) -> list[str]:
    """Buffered stream: one content chunk per choice, a finish chunk, optional usage, [DONE]."""
    base = {
        "id": completion["id"],
        "object": "chat.completion.chunk",
        "created": completion["created"],
        "model": completion["model"],
    }
    chunks: list[str] = []
    finishes: list[tuple[int, Any]] = []
    for pos, choice in enumerate(completion.get("choices", [])):
        index = int(choice.get("index", pos))
        msg = choice.get("message") or {}
        delta: dict[str, Any] = {"role": msg.get("role", "assistant")}
        if msg.get("content") is not None:
            delta["content"] = msg["content"]
        if msg.get("reasoning_content"):
            delta["reasoning_content"] = msg["reasoning_content"]
        calls = msg.get("tool_calls")
        if calls:
            delta["tool_calls"] = [
                {
                    "index": i,
                    "id": c.get("id"),
                    "type": c.get("type", "function"),
                    "function": {
                        "name": (c.get("function") or {}).get("name"),
                        "arguments": (c.get("function") or {}).get("arguments", ""),
                    },
                }
                for i, c in enumerate(calls)
            ]
        chunks.append(
            sse_event(
                {**base, "choices": [{"index": index, "delta": delta, "finish_reason": None}]}
            )
        )
        finishes.append((index, choice.get("finish_reason") or "stop"))
    for index, reason in finishes:
        chunks.append(
            sse_event({**base, "choices": [{"index": index, "delta": {}, "finish_reason": reason}]})
        )
    if include_usage and completion.get("usage") is not None:
        chunks.append(sse_event({**base, "choices": [], "usage": completion["usage"]}))
    chunks.append("data: [DONE]\n\n")
    return chunks


def response_headers(job: sqlite3.Row) -> dict[str, str]:
    h = {"X-LLMHub-Task": str(job["id"])}
    backend = backend_of(job)
    if backend:
        h["X-LLMHub-Backend"] = backend
    if job["fallback_reason"] and job["status"] == "completed":
        h["X-LLMHub-Fallback"] = str(job["fallback_reason"])
    if job["finish_reason"] == "length":
        h["X-LLMHub-Truncated"] = "1"
    if job["est_cost_usd"] is not None:
        h["X-LLMHub-Cost-Estimate"] = f"{float(job['est_cost_usd']):.6f}"
    if job["warnings"]:
        h["X-LLMHub-Warning"] = str(job["warnings"])
    return h


def job_error(job: sqlite3.Row) -> ApiError:
    """Error of a failed/cancelled job."""
    task_id = str(job["id"])
    if job["status"] == "cancelled":
        reason = job["error_code"] if job["error_code"] == "cloud_not_allowed" else None
        return ApiError(ErrorCode.CANCELLED, "task was cancelled", reason=reason, task_id=task_id)
    raw = job["error_code"] or "internal_error"
    code = _KNOWN.get(str(raw))
    if code is None:
        return ApiError(
            ErrorCode.INTERNAL_ERROR, "task failed", reason=str(raw)[:64], task_id=task_id
        )
    reason = job["fallback_reason"]
    messages = {
        ErrorCode.CLOUD_UNAVAILABLE: "cloud GPU unavailable and no local model for this request",
        ErrorCode.LOCAL_UNAVAILABLE: "local model unavailable",
        ErrorCode.QUEUE_WAIT_EXCEEDED: "task waited in the queue longer than allowed",
        ErrorCode.DEADLINE_EXCEEDED: "deadline passed before the task started",
        ErrorCode.UPSTREAM_TIMEOUT: "model server timed out",
        ErrorCode.CONTEXT_TOO_LARGE: "request does not fit the model context",
        ErrorCode.CONTROLLER_HALTED: "service is halted",
    }
    retry = 5.0 if code in (ErrorCode.CLOUD_UNAVAILABLE, ErrorCode.LOCAL_UNAVAILABLE) else None
    if retry is not None and not retryable_for(code, reason):
        retry = None
    return ApiError(
        code, messages.get(code, "task failed"), reason=reason, task_id=task_id,
        retry_after_s=retry,
    )  # fmt: skip
