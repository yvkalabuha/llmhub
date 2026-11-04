"""T-373 A4: tool answer model."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from llmhub.mcp_server.models import AskResult, CheckOut, ErrorInfo


def test_defaults_serialize() -> None:
    data = json.loads(AskResult(verdict="pending", task_id="t1").model_dump_json())
    assert data["verdict"] == "pending" and data["task_id"] == "t1"
    assert data["checks"] == [] and data["warnings"] == [] and data["error"] is None
    assert data["tokens"] == {"prompt": None, "completion": None}
    assert data["truncated_inline"] is False and data["truncated"] is False


def test_mutable_defaults_are_not_shared() -> None:
    a, b = AskResult(verdict="ok"), AskResult(verdict="ok")
    a.checks.append(CheckOut(name="x", ok=True))
    assert b.checks == []


@pytest.mark.parametrize("verdict", ["ok", "check_failed", "pending", "error"])
def test_verdicts(verdict: str) -> None:
    assert AskResult.model_validate({"verdict": verdict}).verdict == verdict


def test_unknown_verdict_rejected() -> None:
    with pytest.raises(ValidationError):
        AskResult.model_validate({"verdict": "maybe"})


def test_error_info() -> None:
    r = AskResult(verdict="error", error=ErrorInfo(code="busy", retryable=True, retry_after_s=1))
    assert r.model_dump()["error"] == {
        "code": "busy",
        "reason": None,
        "retryable": True,
        "retry_after_s": 1.0,
    }
