"""Structured answer of the MCP tools (T-373 plan §3.8)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["ok", "check_failed", "pending", "error"]


class ErrorInfo(BaseModel):
    code: str
    reason: str | None = None
    retryable: bool = False
    retry_after_s: float | None = None


class CheckOut(BaseModel):
    name: str
    ok: bool
    reason: str = ""


class Tokens(BaseModel):
    prompt: int | None = None
    completion: int | None = None


class AskResult(BaseModel):
    verdict: Verdict
    task_id: str | None = None
    status: str | None = None
    eta_s: float | None = None
    text: str | None = None
    output_file: str | None = None
    truncated_inline: bool = False
    truncated: bool = False
    finish_reason: str | None = None
    checks: list[CheckOut] = Field(default_factory=list)
    backend: str | None = None
    fallback: str | None = None
    warnings: list[str] = Field(default_factory=list)
    tokens: Tokens = Field(default_factory=Tokens)
    error: ErrorInfo | None = None
    hint: str | None = None
