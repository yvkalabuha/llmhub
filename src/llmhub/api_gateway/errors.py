"""OpenAI-style error object and the error code table (T-369 plan section 4)."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from llmhub.persistence.db import Busy

log = logging.getLogger("llmhub.api")


class ErrorCode(StrEnum):
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    INVALID_REQUEST = "invalid_request"
    REQUEST_TOO_LARGE = "request_too_large"
    CONTEXT_TOO_LARGE = "context_too_large"
    MODEL_NOT_AVAILABLE = "model_not_available"
    NOT_FOUND = "not_found"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    TASK_NOT_READY = "task_not_ready"
    LEASE_EXPIRED = "lease_expired"
    LEASE_CLOSED = "lease_closed"
    QUEUE_WAIT_EXCEEDED = "queue_wait_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CONTROLLER_HALTED = "controller_halted"
    UPSTREAM_TIMEOUT = "upstream_timeout"
    CANCELLED = "cancelled"
    LOCAL_UNAVAILABLE = "local_unavailable"
    CLOUD_UNAVAILABLE = "cloud_unavailable"
    BUSY = "busy"
    CONFLICT = "conflict"
    INTERNAL_ERROR = "internal_error"


# code -> (HTTP status, retryable, OpenAI "type")
SPEC: dict[ErrorCode, tuple[int, bool, str]] = {
    ErrorCode.UNAUTHORIZED: (401, False, "authentication_error"),
    ErrorCode.FORBIDDEN: (403, False, "permission_error"),
    ErrorCode.INVALID_REQUEST: (400, False, "invalid_request_error"),
    ErrorCode.REQUEST_TOO_LARGE: (413, False, "invalid_request_error"),
    ErrorCode.CONTEXT_TOO_LARGE: (400, False, "invalid_request_error"),
    ErrorCode.MODEL_NOT_AVAILABLE: (404, False, "invalid_request_error"),
    ErrorCode.NOT_FOUND: (404, False, "invalid_request_error"),
    ErrorCode.IDEMPOTENCY_CONFLICT: (409, False, "invalid_request_error"),
    ErrorCode.TASK_NOT_READY: (409, True, "invalid_request_error"),
    ErrorCode.LEASE_EXPIRED: (410, False, "invalid_request_error"),
    ErrorCode.LEASE_CLOSED: (409, False, "invalid_request_error"),
    ErrorCode.CONFLICT: (409, False, "invalid_request_error"),
    ErrorCode.QUEUE_WAIT_EXCEEDED: (503, True, "server_error"),
    ErrorCode.CLOUD_UNAVAILABLE: (503, True, "server_error"),
    ErrorCode.LOCAL_UNAVAILABLE: (503, True, "server_error"),
    ErrorCode.CONTROLLER_HALTED: (503, False, "server_error"),
    ErrorCode.BUSY: (503, True, "server_error"),
    ErrorCode.DEADLINE_EXCEEDED: (504, False, "server_error"),
    ErrorCode.UPSTREAM_TIMEOUT: (504, True, "server_error"),
    ErrorCode.CANCELLED: (409, False, "invalid_request_error"),
    ErrorCode.INTERNAL_ERROR: (500, True, "server_error"),
}

# cloud_unavailable is retryable only for transient causes
_CLOUD_RETRYABLE = {"provider_api_error", "gpu_unavailable", "deployment_error"}


def retryable_for(code: ErrorCode, reason: str | None) -> bool:
    if code is ErrorCode.CLOUD_UNAVAILABLE and reason is not None:
        return reason in _CLOUD_RETRYABLE
    return SPEC[code][1]


class ApiError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        reason: str | None = None,
        retry_after_s: float | None = None,
        task_id: str | None = None,
        fallback: str | None = None,
        headers: Mapping[str, str] | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.retry_after_s = retry_after_s
        self.task_id = task_id
        self.fallback = fallback
        self.headers = dict(headers or {})
        self.status = status if status is not None else SPEC[code][0]

    @property
    def retryable(self) -> bool:
        return retryable_for(self.code, self.reason)

    def body(self) -> dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": SPEC[self.code][2],
                "param": None,
                "code": self.code.value,
                "reason": self.reason,
                "retryable": self.retryable,
                "retry_after_s": self.retry_after_s,
                "task_id": self.task_id,
                "fallback": self.fallback,
            }
        }

    def response_headers(self) -> dict[str, str]:
        h = dict(self.headers)
        if self.retry_after_s is not None and "Retry-After" not in h:
            h["Retry-After"] = str(max(1, math.ceil(self.retry_after_s)))
        if self.code is ErrorCode.UNAUTHORIZED:
            h.setdefault("WWW-Authenticate", "Bearer")
        # openai-python retries 409/5xx on its own unless told otherwise (FN-1)
        h.setdefault("x-should-retry", "true" if self.retryable else "false")
        return h

    def response(self) -> JSONResponse:
        return JSONResponse(self.body(), status_code=self.status, headers=self.response_headers())


def install_handlers(app: FastAPI) -> None:
    async def on_api_error(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, ApiError)
        return exc.response()

    async def on_validation(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        # field paths only: the request text is never echoed
        locs = sorted({".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()})
        return ApiError(
            ErrorCode.INVALID_REQUEST, "invalid request: " + ", ".join(locs)[:300]
        ).response()

    async def on_http(_request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, StarletteHTTPException)
        if exc.status_code == 404:
            return ApiError(ErrorCode.NOT_FOUND, "not found").response()
        if exc.status_code == 405:
            return ApiError(
                ErrorCode.INVALID_REQUEST, "method not allowed", status=405,
                headers={k: v for k, v in (exc.headers or {}).items()},
            ).response()  # fmt: skip
        return ApiError(ErrorCode.INVALID_REQUEST, "bad request", status=exc.status_code).response()

    async def on_busy(_request: Request, _exc: Exception) -> JSONResponse:
        return ApiError(ErrorCode.BUSY, "database busy, retry", retry_after_s=1).response()

    async def on_any(_request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled error: %s", type(exc).__name__)
        return ApiError(ErrorCode.INTERNAL_ERROR, "internal error").response()

    app.add_exception_handler(ApiError, on_api_error)
    app.add_exception_handler(RequestValidationError, on_validation)
    app.add_exception_handler(StarletteHTTPException, on_http)
    app.add_exception_handler(Busy, on_busy)
    app.add_exception_handler(Exception, on_any)
