"""G2: parse_timings of llama-server responses, MTP included; tolerant to garbage."""

from __future__ import annotations

from typing import Any

import pytest

from i_infer_fake import LLAMA_TIMINGS
from llmhub.cloud_manager.http_inference import Timings, parse_timings


def test_full_mtp_timings() -> None:
    t = parse_timings({"timings": dict(LLAMA_TIMINGS)})
    assert t == Timings(1200, 1000.0, 78, 1000.0, 60, 45)
    assert t is not None
    assert t.pp_tps == pytest.approx(1200.0)
    assert t.tg_tps == pytest.approx(78.0)
    assert t.accept_rate == pytest.approx(0.75)


def test_without_mtp_draft_fields_none() -> None:
    raw = {k: v for k, v in LLAMA_TIMINGS.items() if not k.startswith("draft")}
    t = parse_timings({"timings": raw})
    assert t is not None and t.draft_n is None and t.draft_n_accepted is None
    assert t.accept_rate is None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"timings": None},
        {"timings": "x"},
        {"timings": {"prompt_n": "12", "prompt_ms": 1, "predicted_n": 1, "predicted_ms": 1}},
        {"timings": {"prompt_n": -1, "prompt_ms": 1, "predicted_n": 1, "predicted_ms": 1}},
        {"timings": {"prompt_n": 1.5, "prompt_ms": 1, "predicted_n": 1, "predicted_ms": 1}},
        {"timings": {"prompt_n": True, "prompt_ms": 1, "predicted_n": 1, "predicted_ms": 1}},
        {
            "timings": {
                "prompt_n": 1,
                "prompt_ms": float("nan"),
                "predicted_n": 1,
                "predicted_ms": 1,
            }
        },
        {"timings": {"prompt_n": 1, "prompt_ms": 1}},
        [],
        "garbage",
        None,
    ],
)
def test_garbage_is_none(body: Any) -> None:
    assert parse_timings(body) is None


def test_zeros_do_not_divide_by_zero() -> None:
    t = parse_timings(
        {
            "timings": {
                "prompt_n": 0,
                "prompt_ms": 0,
                "predicted_n": 0,
                "predicted_ms": 0,
                "draft_n": 0,
                "draft_n_accepted": 0,
            }
        }
    )
    assert t is not None and t.pp_tps == 0.0 and t.tg_tps == 0.0 and t.accept_rate is None


def test_bad_draft_fields_become_none() -> None:
    raw = dict(LLAMA_TIMINGS) | {"draft_n": "x", "draft_n_accepted": -3}
    t = parse_timings({"timings": raw})
    assert t is not None and t.draft_n is None and t.draft_n_accepted is None
    t2 = parse_timings({"timings": dict(LLAMA_TIMINGS) | {"draft_n_accepted": 99}})
    assert t2 is not None and t2.accept_rate == 1.0  # clamped
