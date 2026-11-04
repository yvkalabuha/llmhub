"""T-372 A3: RunPod error normalization (plan 3.4); messages carry no secret and no body."""

from __future__ import annotations

import pytest

from llmhub.provider_adapters.base import UnavailableReason as R
from llmhub.provider_adapters.runpod import normalize_runpod_error
from llmhub.provider_adapters.runpod_api import RunpodHttpError, RunpodParseError, classify


def _e(status: int | None, code: str | None = None, *, sent: bool = True,
       retry_after: float | None = None) -> RunpodHttpError:  # fmt: skip
    return RunpodHttpError(method="POST", path="/pods", status=status, code=code,
                           retry_after_s=retry_after, sent=sent)  # fmt: skip


@pytest.mark.parametrize(
    ("exc", "reason", "retryable", "unknown"),
    [
        (_e(401), R.PROVIDER_API_ERROR, False, False),
        (_e(403), R.PROVIDER_API_ERROR, False, False),
        (_e(402), R.INSUFFICIENT_BALANCE, False, False),
        (_e(400, "insufficient_balance"), R.INSUFFICIENT_BALANCE, False, False),
        (_e(200, "insufficient_balance"), R.INSUFFICIENT_BALANCE, False, False),
        (_e(500, "no_capacity"), R.GPU_UNAVAILABLE, True, False),
        (_e(409, "no_capacity"), R.GPU_UNAVAILABLE, True, False),
        (_e(503, "no_capacity"), R.GPU_UNAVAILABLE, True, False),
        (_e(400, "spend_limit"), R.QUOTA_EXCEEDED, False, False),
        (_e(429, retry_after=7), R.PROVIDER_API_ERROR, True, False),
        (_e(400), R.DEPLOYMENT_ERROR, False, False),
        (_e(422), R.DEPLOYMENT_ERROR, False, False),
        (_e(500), R.PROVIDER_API_ERROR, True, True),
        (_e(502), R.PROVIDER_API_ERROR, True, True),
        (_e(None, "timeout"), R.PROVIDER_API_ERROR, True, True),
        (_e(None, "transport"), R.PROVIDER_API_ERROR, True, True),
        (_e(None, "connect", sent=False), R.PROVIDER_API_ERROR, True, False),
        (_e(None, "key_unavailable", sent=False), R.PROVIDER_API_ERROR, True, False),
        (RunpodParseError(method="POST", path="/pods", status=None, code="parse:id",
                          retry_after_s=None, sent=True), R.PROVIDER_API_ERROR, False, True),
        (ValueError("x"), R.PROVIDER_API_ERROR, True, True),
        (TimeoutError("x"), R.PROVIDER_API_ERROR, True, True),
    ],
)  # fmt: skip
def test_table(exc: BaseException, reason: R, retryable: bool, unknown: bool) -> None:
    err = normalize_runpod_error(exc)
    assert (err.reason, err.retryable, err.outcome_unknown) == (reason, retryable, unknown)


def test_retry_after_and_status_kept() -> None:
    err = normalize_runpod_error(_e(429, retry_after=7.5))
    assert err.retry_after_s == 7.5 and err.http_status == 429


def test_message_has_no_secret() -> None:
    err = normalize_runpod_error(ValueError("boom api_key=rpa_SECRET-VALUE"))
    assert "rpa_SECRET" not in err.message and err.message == "ValueError"
    err2 = normalize_runpod_error(_e(400, "insufficient_balance"))
    assert err2.message == "POST /pods -> 400 insufficient_balance"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("There are no longer any instances available with the requested specifications",
         "no_capacity"),
        ('{"error":"Insufficient balance to deploy"}', "insufficient_balance"),
        ("Your spend limit was reached", "spend_limit"),
        ("something else", None),
    ],
)  # fmt: skip
def test_classify(text: str, code: str | None) -> None:
    assert classify(text) == code
