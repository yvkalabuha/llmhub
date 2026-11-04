"""T-370 review round 2: unit probes of the round-1 fixes (SSE assembly, load keys)."""

from __future__ import annotations

import json

import pytest

from llmhub.cloud_manager.http_inference import assemble_sse


def _ev(obj: dict[str, object]) -> bytes:
    # llama-server (nlohmann::json, ensure_ascii=false) writes non-ASCII characters raw
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"


def _delta(text: str, fr: str | None = None) -> bytes:
    return _ev({"id": "c", "choices": [{"index": 0, "delta": {"content": text},
                                       "finish_reason": fr}]})  # fmt: skip


@pytest.mark.parametrize("sep", [" ", " ", "\x85"])
def test_unicode_line_separators_inside_content_are_kept(sep: str) -> None:
    """R2-F1: str.splitlines() also splits on U+2028/U+2029/U+0085, which JSON leaves raw.
    SSE lines end only with LF/CR, so the delta must survive."""
    raw = _delta("a") + _delta(f"b{sep}c") + _delta("", "stop") + b"data: [DONE]\n\n"
    data, complete = assemble_sse(raw)
    assert complete and data is not None
    assert data["choices"][0]["message"]["content"] == f"ab{sep}c"


def test_crlf_framing_is_accepted() -> None:
    raw = (_delta("x") + _delta("", "stop")).replace(b"\n", b"\r\n") + b"data: [DONE]\r\n\r\n"
    data, complete = assemble_sse(raw)
    assert complete and data is not None and data["choices"][0]["message"]["content"] == "x"


def test_llama_style_tool_call_deltas_and_usage_chunk() -> None:
    """llama-server streams the tool name first, then argument pieces; usage/timings come in a
    final chunk with an empty `choices` list."""
    raw = (
        _ev({"choices": [{"index": 0, "delta": {"role": "assistant", "content": None}}]})
        + _ev({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "t1",
              "type": "function", "function": {"name": "f", "arguments": ""}}]}}]})
        + _ev({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
              "function": {"arguments": '{"a":'}}]}}]})
        + _ev({"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0,
              "function": {"arguments": "1}"}}]}}]})
        + _ev({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        + _ev({"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 7},
               "timings": {"prompt_n": 5, "predicted_n": 7}})
        + b"data: [DONE]\n\n"
    )  # fmt: skip
    data, complete = assemble_sse(raw)
    assert complete and data is not None
    ch = data["choices"][0]
    assert ch["finish_reason"] == "tool_calls"
    assert ch["message"]["tool_calls"] == [
        {"id": "t1", "type": "function", "function": {"name": "f", "arguments": '{"a":1}'}}
    ]
    assert data["usage"] == {"prompt_tokens": 5, "completion_tokens": 7}
    assert data["timings"]["predicted_n"] == 7


def test_data_error_event_mid_stream() -> None:
    raw = _delta("partial") + _ev({"error": {"code": 500, "message": "kv full"}})
    data, complete = assemble_sse(raw)
    assert complete and data == {"error": {"code": 500, "message": "kv full"}}


# --- F4 race: the instance leaves LOADING_MODEL while the pod is being polled ----------------
async def test_no_orphan_progress_key_when_instance_leaves_loading_during_poll(
    tmp_path: object,
) -> None:
    """R2-F4: `_load_progress` awaits the pod, then writes `load_progress:<iid>`. If another
    actor (admin kill, mark_lost) moved the instance out of LOADING_MODEL meanwhile, the key
    must not be written after transition() already dropped it."""
    from pathlib import Path

    from harness import make_job
    from llmhub.lifecycle_manager import machine
    from llmhub.provider_adapters.mock_inference import MockInferenceKnobs
    from test_review_t370_fn_loading import _load_keys
    from test_w_provisioner_loading import make_h

    assert isinstance(tmp_path, Path)
    h = make_h(tmp_path, knobs=MockInferenceKnobs(hang_load=True, load_sources_left=1))
    inner = h.inference.load_status
    killed: list[str] = []

    async def racing(endpoint: str):  # type: ignore[no-untyped-def]
        st = await inner(endpoint)
        row = h.conn.execute(
            "SELECT id FROM instances WHERE endpoint=? AND state='LOADING_MODEL'", (endpoint,)
        ).fetchone()
        if row is not None and not killed:
            killed.append(row["id"])
            machine.mark_lost(h.conn, row["id"], h.clock.now(), "admin_kill", cfg=h.cfg)
        return st

    h.inference.load_status = racing  # type: ignore[method-assign]
    h.submit(make_job(0))
    await h.run_until(h.clock.now() + 120)
    assert killed
    assert _load_keys(h) == 0


# --- F6 regression: no brake on repeated weights failures -------------------------------------
# R2-1 (major, money): `exhausted` was the pod's top-level reason for EVERY download failure
# and F6 never blacklisted it: 3 paid launches per demand wave, all on one host (12 in 4 waves).
async def test_repeated_weights_failures_are_braked(tmp_path: object) -> None:
    from pathlib import Path

    from harness import make_job
    from llmhub.provider_adapters.mock_inference import MockInferenceKnobs
    from test_w_provisioner_loading import make_h

    assert isinstance(tmp_path, Path)
    h = make_h(tmp_path, knobs=MockInferenceKnobs(load_fail="exhausted"))
    for wave in range(4):
        h.submit(make_job(wave))
        await h.run_until(h.clock.now() + 3600)
    launches = h.conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
    # with a host blacklist (e.g. reason `disk`) the same scenario stops at 6 (3 mock hosts)
    assert launches <= 6
