"""T-369 W3: X-LLMHub-* headers and data_class narrowing."""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest

from api_fixtures import api_config
from llmhub.api_gateway.auth import Principal
from llmhub.api_gateway.errors import ApiError
from llmhub.api_gateway.options import parse_options
from llmhub.task_queue.models import Priority


def principal(cloud: bool = True, max_prio: Priority = Priority.NORMAL) -> Principal:
    return Principal(
        client_id="c", allowed_models=frozenset({"*"}), max_priority=max_prio,
        default_priority=Priority.NORMAL, default_policy="BALANCED", cloud_allowed=cloud,
    )  # fmt: skip


def opts(tmp_path: Path, headers: dict[str, str] | None = None, p: Principal | None = None,
         overrides: dict[str, Any] | None = None):  # type: ignore[no-untyped-def]  # fmt: skip
    return parse_options(headers or {}, p or principal(), api_config(tmp_path), overrides)


def test_defaults(tmp_path: Path) -> None:
    o = opts(tmp_path)
    assert (o.priority, o.policy, o.deadline_in_s, o.wait_s) == (
        Priority.NORMAL,
        "BALANCED",
        None,
        480,
    )
    assert o.data_class == "cloud_allowed" and o.task_type is None and o.idem_key is None


def test_each_header(tmp_path: Path) -> None:
    o = opts(tmp_path, {
        "X-LLMHub-Priority": "low", "X-LLMHub-Policy": "quality", "X-LLMHub-Deadline": "90",
        "X-LLMHub-Wait": "30", "X-LLMHub-Task-Type": "summarize", "X-LLMHub-Lease": "L1",
        "Idempotency-Key": "abc-123",
    })  # fmt: skip
    assert o.priority is Priority.LOW and o.policy == "QUALITY" and o.deadline_in_s == 90
    assert o.wait_s == 30 and o.task_type == "summarize" and o.lease_id == "L1"
    assert o.idem_key == "abc-123"


def test_priority_above_max_is_403(tmp_path: Path) -> None:
    with pytest.raises(ApiError) as ei:
        opts(tmp_path, {"X-LLMHub-Priority": "urgent"})
    assert ei.value.status == 403 and ei.value.reason == "priority_not_allowed"
    assert opts(tmp_path, {"X-LLMHub-Priority": "urgent"},
                principal(max_prio=Priority.URGENT)).priority is Priority.URGENT  # fmt: skip


def test_overrides_beat_headers(tmp_path: Path) -> None:
    o = opts(tmp_path, {"X-LLMHub-Priority": "low", "X-LLMHub-Policy": "ECONOMY"},
             overrides={"priority": "normal", "policy": "QUALITY", "deadline_s": 5})  # fmt: skip
    assert o.priority is Priority.NORMAL and o.policy == "QUALITY" and o.deadline_in_s == 5


@pytest.mark.parametrize(
    "headers",
    [
        {"X-LLMHub-Wait": "0"},
        {"X-LLMHub-Wait": "481"},
        {"X-LLMHub-Wait": "abc"},
        {"X-LLMHub-Wait": "nan"},
        {"X-LLMHub-Task-Type": "unknown"},
        {"X-LLMHub-Priority": "bogus"},
        {"X-LLMHub-Policy": "FAST"},
        {"X-LLMHub-Deadline": "-1"},
        {"X-LLMHub-Deadline": "x"},
        {"X-LLMHub-Data-Class": "public"},
        {"Idempotency-Key": ""},
        {"Idempotency-Key": "a" * 256},
        {"Idempotency-Key": "bad key"},
        {"Idempotency-Key": "ключ"},
    ],
)
def test_bad_values_are_400(tmp_path: Path, headers: dict[str, str]) -> None:
    with pytest.raises(ApiError) as ei:
        opts(tmp_path, headers)
    assert ei.value.status == 400


def test_wait_bounds_ok(tmp_path: Path) -> None:
    assert opts(tmp_path, {"X-LLMHub-Wait": "1"}).wait_s == 1
    assert opts(tmp_path, {"X-LLMHub-Wait": "480"}).wait_s == 480
    assert opts(tmp_path, {"Idempotency-Key": "a" * 255}).idem_key == "a" * 255


@pytest.mark.parametrize(
    ("client", "header", "field"),
    list(
        itertools.product(
            [True, False], [None, "private", "cloud_allowed"], [None, "private", "cloud_allowed"]
        )
    ),
)
def test_data_class_only_narrows(
    tmp_path: Path, client: bool, header: str | None, field: str | None
) -> None:
    headers = {} if header is None else {"X-LLMHub-Data-Class": header}
    ov = None if field is None else {"data_class": field}
    got = opts(tmp_path, headers, principal(cloud=client), ov).data_class
    wide = client and header != "private" and field != "private"
    assert got == ("cloud_allowed" if wide else "private")
